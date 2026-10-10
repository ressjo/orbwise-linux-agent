"""Risikobewertung von Shell-Befehlen.

safe     – nur lesend, wird direkt ausgeführt
confirm  – verändert etwas / unbekannt / Root → Nutzer muss bestätigen
blocked  – zerstörerisch, wird nie ausgeführt
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path

from ..lang import T
from .registry import BLOCKED, CONFIRM, SAFE
from .secretpaths import contains_secrets, expand_arg, is_secret_path

SEPARATORS = {";", "&&", "||", "|", "&", "\n", "|&", ";;", "(", ")"}
REDIRECTS = {">", ">>", ">|", "&>", "&>>"}
ROOT_WRAPPERS = {"sudo", "doas", "pkexec", "su", "run0"}
TRANSPARENT_WRAPPERS = {"nice", "time", "command", "nohup", "ionice", "stdbuf"}

SAFE_COMMANDS = {
    "ls", "cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "fd", "locate", "plocate", "which",
    "whereis", "type", "file", "stat", "wc", "sort", "uniq", "cut", "tr", "echo", "printf", "pwd", "date",
    "cal", "uptime", "uname", "hostname", "whoami", "id", "groups", "df", "du", "free", "lsblk", "lscpu",
    "lsusb", "lspci", "lsmod", "findmnt", "ps", "pgrep", "sensors", "ss", "nslookup", "dig", "host",
    "journalctl", "checkupdates", "tree", "basename", "dirname", "realpath", "readlink",
    "locale", "inxi", "fastfetch", "neofetch", "nvidia-smi", "rocm-smi", "rocminfo", "glxinfo",
    "vulkaninfo", "nproc", "getconf", "md5sum", "sha1sum", "sha256sum", "column", "jq", "diff", "cmp",
    "zcat", "xxd", "hexdump", "strings", "cd", "true", "test", "[", "lsof", "vmstat", "iostat", "w",
    "last", "timedatectl", "hostnamectl", "loginctl", "iwconfig", "xdg-mime", "tldr", "less",
    "more", "xrandr", "fc-list", "pactl", "wpctl", "amixer", "lsb_release",
    # weitere rein lesende Werkzeuge (stehen nur hier im Code – kosten keinen Kontext im Prompt)
    "who", "users", "lastlog", "pstree", "pidof", "mpstat", "pidstat", "lsns", "lslocks", "lsipc", "zramctl",
    "nl", "tac", "rev", "fold", "fmt", "expand", "comm", "join", "paste", "seq", "factor",
    "sha224sum", "sha384sum", "sha512sum", "b2sum", "cksum", "sum", "base32", "od", "zgrep", "zless", "xzcat",
    "bzcat", "zstdcat", "lzcat", "bat", "batcat", "eza", "exa", "lsd", "ag", "ack", "yq", "tracepath", "traceroute",
    "mtr", "whois", "arp", "netstat", "getent", "lsattr", "getfacl", "namei", "lshw", "hwinfo", "dmidecode",
    "upower", "acpi", "vainfo", "vdpauinfo", "clinfo", "xdpyinfo", "fc-match", "pw-dump", "lsinitcpio",
    "sestatus", "getenforce", "aa-status", "apparmor_status", "systemd-analyze",
}

# Befehle mit Unterbefehlen: nur diese (lesenden) Unterbefehle gelten als sicher
SAFE_SUBCOMMANDS = {
    "docker": {"ps", "images", "image", "inspect", "logs", "stats", "version", "info", "top", "port", "diff",
               "history", "volume", "network", "system", "compose", "container", "search"},
    "podman": {"ps", "images", "image", "inspect", "logs", "stats", "version", "info", "top", "port", "diff",
               "history", "volume", "network", "system", "compose", "container", "search", "pod"},
    "kubectl": {"get", "describe", "logs", "top", "version", "explain", "api-resources", "cluster-info"},
    "virsh": {"list", "dominfo", "domstate", "domifaddr", "nodeinfo", "net-list", "pool-list", "vol-list", "version"},
    "apt": {"list", "show", "search", "policy", "depends", "rdepends", "showsrc", "changelog"},
    "apt-cache": {"show", "search", "policy", "depends", "rdepends", "showpkg", "stats", "madison", "pkgnames"},
    "dnf": {"list", "info", "search", "repolist", "check-update", "provides", "repoquery", "history", "deplist"},
    "yum": {"list", "info", "search", "repolist", "check-update", "provides", "history"},
    "zypper": {"search", "se", "info", "if", "list-updates", "lu", "repos", "lr", "packages", "pa", "patches"},
    "ollama": {"list", "ls", "ps", "show", "--version", "-v"},
    "pip": {"list", "show", "freeze", "check", "--version", "-V"},
    "pip3": {"list", "show", "freeze", "check", "--version", "-V"},
    "npm": {"ls", "list", "outdated", "view", "--version", "-v"},
    "hyprctl": {"clients", "monitors", "activewindow", "activeworkspace", "workspaces", "devices", "version",
                "layers", "binds", "getoption", "instances", "systeminfo"},
    "gnome-extensions": {"list", "info", "show", "version"},
    "bluetoothctl": {"show", "devices", "info", "list", "paired-devices"},
    "powerprofilesctl": {"get", "list", ""},
    "brightnessctl": {"get", "info", "-l", "--list", "i", "g", ""},
    "gsettings": {"get", "list-schemas", "list-keys", "list-recursively", "list-children", "range", "describe"},
    "tmux": {"ls", "list-sessions", "list-windows"},
    "nvme": {"list", "list-subsys", "smart-log", "id-ctrl", "id-ns", "error-log", "fw-log"},
    "fwupdmgr": {"get-devices", "get-updates", "get-upgrades", "get-history", "get-remotes", "security", "get-releases"},
    "tailscale": {"status", "ip", "version", "netcheck", "whois"},
    "zerotier-cli": {"info", "listnetworks", "listpeers", "peers", "status"},
    "wg": {"show", ""},
    "busctl": {"list", "status", "tree", "introspect"},
    "resolvectl": {"status", "query", "statistics", ""},
    "networkctl": {"list", "status", "lldp", ""},
    "coredumpctl": {"list", "info", ""},
    "localectl": {"status", "list-locales", "list-keymaps", "list-x11-keymap-layouts", ""},
    "pw-cli": {"ls", "list-objects", "info", "i"},
    "iw": {"dev", "list", "phy", "reg"},
}
# Sicher, solange keine dieser Optionen vorkommt
FORBIDDEN_FLAGS = {
    "dmesg": {"-C", "--clear", "-c", "--read-clear", "-D", "--console-off", "-E", "--console-on", "-n"},
    "fuser": {"-k", "--kill"},
    "crontab": {"-r", "-e", "-i"},
    "smartctl": {"-s", "--smart", "-t", "--test", "-o", "--offlineauto", "-S", "--saveauto", "-X", "--abort"},
}
# Sicher nur mit genau diesen (lesenden) Optionen
ONLY_FLAGS = {
    "efibootmgr": {"-v", "--verbose"},
    "hdparm": {"-I", "-i", "-g", "-C"},
    "arecord": {"-l", "-L", "--list-devices", "--list-pcms"},
    "aplay": {"-l", "-L", "--list-devices", "--list-pcms"},
    "v4l2-ctl": {"--list-devices", "--all", "-l", "--list-formats", "--list-formats-ext", "-D", "--info"},
    "kscreen-doctor": {"-o", "--outputs", "-j", "--json"},
    "setxkbmap": {"-query", "-print"},
    "xset": {"q", "-q"},
    "wlr-randr": {"--json"},
    "xprop": {"-root"},
}

CRITICAL_PATHS = {"/", "/*", "~", "~/", "~/*", "$HOME", "${HOME}", "/home", "/etc", "/usr", "/boot", "/var",
                  "/bin", "/sbin", "/lib", "/lib64", "/opt", "/root", "/srv", "/dev", "/proc", "/sys", "/mnt",
                  "*", ".", "./", "./*", "..", "../", str(Path.home())}

BLOCK_PATTERNS = [
    (re.compile(r"--no-preserve-root"), lambda: T("Löschen des Wurzelverzeichnisses", "deleting the root directory")),
    (re.compile(r"(^|[\s;&|(])mkfs(\.\w+)?\b"), lambda: T("Formatieren eines Dateisystems", "formatting a file system")),
    (re.compile(r"\bdd\b[^;&|]*\bof=/dev/(sd|nvme|hd|vd|mmcblk|disk)"),
     lambda: T("Überschreiben eines Datenträgers", "overwriting a disk")),
    (re.compile(r">\s*/dev/(sd|nvme|hd|vd|mmcblk)"), lambda: T("Überschreiben eines Datenträgers", "overwriting a disk")),
    (re.compile(r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"), lambda: T("Fork-Bombe", "fork bomb")),
    (re.compile(r"\b(wipefs|shred|blkdiscard)\b[^;&|]*/dev/"), lambda: T("Löschen eines Datenträgers", "wiping a disk")),
    (re.compile(r"\bch(mod|own|grp)\b[^;&|]*\s-\w*R\w*\b[^;&|]*\s/(\s|$|\*)"),
     lambda: T("Rechte des ganzen Systems ändern", "changing permissions of the whole system")),
]

# Befehle, die Dateiinhalte ausgeben – auf Schlüssel/Passwort-Dateien angewandt nur mit Rückfrage
READERS = {"cat", "head", "tail", "grep", "egrep", "fgrep", "rg", "less", "more", "zcat", "xxd", "hexdump",
           "strings", "jq", "diff", "cmp", "sort", "uniq", "cut", "column", "tr", "sed", "awk", "base64", "od",
           "nl", "tac", "bat", "batcat", "view", "vim", "vi", "nano", "cp", "scp", "rsync", "tar", "zip", "curl"}
SECRET_VARS = re.compile(r"\$\{?\w*(TOKEN|PASSW|SECRET|API_?KEY|PRIVATE)\w*", re.I)
RECURSIVE_FLAGS = {"-r", "-R", "--recursive", "--dereference-recursive"}

WARN_PATTERNS = [
    (re.compile(r"\b(curl|wget)\b[^;&]*\|\s*(sudo\s+)?(ba|z|da|fi)?sh\b"),
     lambda: T("führt ein Skript direkt aus dem Internet aus", "runs a script straight from the internet")),
    (re.compile(r"\b(fdisk|parted|sgdisk|gdisk|cfdisk)\b"), lambda: T("Partitionierungswerkzeug", "partitioning tool")),
    (re.compile(r"\bpacman\b[^;&|]*\s-R\w*d\w*d"),
     lambda: T("entfernt Pakete ohne Abhängigkeitsprüfung", "removes packages without dependency checks")),
]


_HEREDOC = re.compile(r"<<(-?)[ \t]*(['\"]?)([\w.-]+)\2")


def strip_heredocs(cmd: str) -> str:
    """Den Text von Heredocs (cat > x.sh <<'EOF' … EOF) entfernen – das ist Inhalt, kein Befehl. Bei ungequotetem
    Begrenzer bleibt eine Befehlsersetzung im Text ($(…), `…`) als Platzhalter stehen, damit sie auffällt."""
    out, pos = [], 0
    for m in _HEREDOC.finditer(cmd):
        if m.start() < pos:
            continue
        nl = cmd.find("\n", m.end())
        if nl < 0:
            break
        strip_tabs, quoted, word = m.group(1) == "-", bool(m.group(2)), m.group(3)
        end = re.compile(r"^" + ("\t*" if strip_tabs else "") + re.escape(word) + r"[ \t]*$", re.M)
        e = end.search(cmd, nl + 1)
        body_end = e.start() if e else len(cmd)
        body = cmd[nl + 1:body_end]
        out.append(cmd[pos:nl + 1])
        if not quoted and ("$(" in body or "`" in body):  # als No-op-Argument behalten: $(…) wird mitgeprüft
            out.append(': "' + body.replace("\\", "\\\\").replace('"', '\\"') + '"\n')
        pos = e.end() if e else len(cmd)
    out.append(cmd[pos:])
    return "".join(out)


def _tokens(cmd: str) -> list[str]:
    """Wörter und Operatoren; Zeilenumbrüche trennen Befehle wie ';' (sonst wäre „ls⏎rm -rf ~“ ein harmloses ls)."""
    cmd = strip_heredocs(cmd.replace("\\\n", " "))
    lex = shlex.shlex(cmd, posix=True, punctuation_chars=";&|()<>\n")
    lex.whitespace = " \t\r"
    lex.whitespace_split = True
    lex.commenters = ""
    out = []
    for t in lex:
        if "\n" in t and not t.strip(";&|()<>\n"):  # Operator-Gruppe mit Zeilenumbruch, z. B. „&&⏎“ oder „⏎⏎“
            rest = t.replace("\n", "")
            out.extend([rest, "\n"] if rest else ["\n"])
        else:
            out.append(t)
    return out


def _segments(tokens: list[str]) -> list[list[str]]:
    segs, cur = [], []
    for t in tokens:
        if t in SEPARATORS:
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        segs.append(cur)
    return segs


def _strip_wrappers(seg: list[str]) -> tuple[list[str], bool]:
    """Entfernt VAR=wert, env, nice, timeout … und erkennt sudo/pkexec."""
    root = False
    i = 0
    while i < len(seg):
        t = seg[i]
        base = os.path.basename(t)
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t):
            i += 1
        elif base in ROOT_WRAPPERS:
            root = True
            i += 1
            while i < len(seg) and seg[i].startswith("-"):
                i += 2 if seg[i] in ("-u", "-g", "--user") else 1
        elif base == "env" or base in TRANSPARENT_WRAPPERS:
            i += 1
            while i < len(seg) and seg[i].startswith("-"):
                i += 1
        elif base == "timeout":
            i += 1
            while i < len(seg) and seg[i].startswith("-"):
                i += 1
            i += 1  # Dauer
        else:
            break
    return seg[i:], root


def _rm_is_catastrophic(args: list[str]) -> bool:
    recursive = any(a == "--recursive" or (a.startswith("-") and not a.startswith("--") and "r" in a.lower())
                    for a in args)
    targets = {a.rstrip("/") or "/" for a in args if not a.startswith("-")}
    targets |= {a for a in args if not a.startswith("-")}
    return recursive and bool(targets & CRITICAL_PATHS)


def _segment_is_safe(cmd: str, args: list[str]) -> bool:
    if cmd == "find":
        return not any(a in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls")
                       for a in args)
    if cmd == "sed":
        return not any(a.startswith("-i") or a.startswith("--in-place") for a in args)
    if cmd == "top":
        return "-b" in args or any(a.startswith("-b") for a in args)
    if cmd == "ping":
        return any(a.startswith("-c") for a in args)
    if cmd in ("pacman", "yay", "paru"):
        op = next((a for a in args if a.startswith("-")), "")
        if op.startswith("-Q") or op.startswith("--query"):
            return True
        if op.startswith("-S") and len(op) > 2 and set(op[2:]) <= set("silg"):
            return True
        if op.startswith("-F") and "y" not in op:
            return True
        return op in ("--version", "-V")
    if cmd == "systemctl":
        sub = next((a for a in args if not a.startswith("-")), "")
        return sub in ("status", "list-units", "list-unit-files", "list-timers", "is-active", "is-enabled",
                       "is-failed", "show", "cat", "list-dependencies", "list-sockets", "list-jobs",
                       "list-machines", "get-default", "is-system-running") or (not sub and "--failed" in args)
    if cmd == "ip":
        words = [a for a in args if not a.startswith("-")]
        return bool(words) and words[0] in ("a", "addr", "address", "l", "link", "r", "route", "n", "neigh") \
            and not any(w in ("add", "del", "delete", "set", "flush", "change", "replace") for w in words[1:])
    if cmd == "git":
        sub = next((a for a in args if not a.startswith("-")), "")
        if sub in ("status", "log", "diff", "show", "ls-files", "blame", "rev-parse", "shortlog", "describe",
                   "reflog", "ls-remote", "grep", "whatchanged", "cat-file", "ls-tree", "count-objects"):
            return True
        if sub not in ("branch", "remote", "tag", "stash"):
            return False
    if cmd in ("flatpak", "snap"):
        sub = next((a for a in args if not a.startswith("-")), "")
        return sub in ("list", "info", "search", "find")
    if cmd == "dpkg":
        return any(a in ("-l", "--list", "-L", "--listfiles", "-s", "--status", "-S", "--search", "-p", "--print-avail",
                         "--get-selections", "--print-architecture") for a in args)
    if cmd == "rpm":
        op = next((a for a in args if a.startswith("-")), "")
        return op.startswith("-q") or op in ("--query", "-V", "--verify")
    if cmd in ("journalctl",):
        return not any(a.startswith("--vacuum") or a in ("--rotate", "--flush") for a in args)
    if cmd in ("pactl", "wpctl", "amixer", "xrandr", "timedatectl", "hostnamectl", "loginctl"):
        sub = next((a for a in args if not a.startswith("-")), "")
        return sub in ("", "status", "list", "info", "show", "get", "inspect", "get-volume", "scontents")
    if cmd == "nmcli":  # nmcli [Objekt] [Verb]: nur anzeigen (device status, connection show, general, radio …)
        words = [a for a in args if not a.startswith("-")]
        obj = words[0] if words else ""
        verb = words[1] if len(words) > 1 else ""
        if obj in ("", "g", "ge", "general", "n", "networking", "r", "radio", "m", "monitor"):
            return verb in ("", "status", "permissions", "hostname", "logging", "connectivity", "all", "wifi", "wwan") \
                and len(words) <= 2
        if obj in ("d", "dev", "device", "c", "con", "connection"):
            if verb == "wifi":
                return len(words) <= 3 and (words[2] if len(words) > 2 else "list") in ("list", "show-password") \
                    and "show-password" not in words
            return verb in ("", "status", "show", "s", "list", "monitor")
        return False
    if cmd in SAFE_SUBCOMMANDS:
        sub = next((a for a in args if not a.startswith("-")), "")
        if sub not in SAFE_SUBCOMMANDS[cmd] and not (sub == "" and "" in SAFE_SUBCOMMANDS[cmd]):
            return sub == "" and any(a in ("--version", "-v", "-V", "--help") for a in args)
        words = [a for a in args if not a.startswith("-")]
        if cmd in ("docker", "podman"):  # z. B. docker volume ls, docker compose ps – keine rm/prune/up …
            return all(w not in ("rm", "rmi", "prune", "create", "run", "exec", "kill", "stop", "start", "restart",
                                 "up", "down", "pull", "push", "build", "pause", "unpause", "update", "connect",
                                 "disconnect", "cp", "commit", "tag", "load", "save", "import") for w in words[1:])
        if cmd == "gsettings":
            return "set" not in words and "reset" not in words
        if cmd == "iw":  # iw dev / iw dev wlan0 link / iw list – kein set/connect/del …
            return not any(w in ("set", "connect", "disconnect", "del", "add", "ibss", "mesh", "join", "leave",
                                 "interface", "switch", "offchannel", "vendor", "wowlan", "coalesce") for w in words)
        if cmd == "wg":
            return len(words) <= 2 and not any(w in ("private-key", "preshared-keys") for w in words)
        return True
    if cmd in ONLY_FLAGS:  # Geräte/Pfade als Argument erlaubt, Optionen nur aus der Liste
        flags = [a for a in args if a.startswith("-") or cmd in ("xset",)]
        return all(a in ONLY_FLAGS[cmd] for a in flags) and (cmd not in ("hdparm", "setxkbmap", "xset", "arecord", "aplay", "v4l2-ctl") or bool(flags))
    if cmd in FORBIDDEN_FLAGS:
        return not any(a in FORBIDDEN_FLAGS[cmd] or any(a.startswith(f + "=") for f in FORBIDDEN_FLAGS[cmd])
                       for a in args)
    if cmd == "wmctrl":  # -l/-d/-m listen nur; -c/-k/-r … ändern Fenster
        return bool(args) and all(a in ("-l", "-d", "-m", "-p", "-G", "-x", "-lp", "-lx", "-lG") for a in args)
    if cmd == "xdotool":
        return bool(args) and (args[0].startswith("get") or args[0] in ("search", "version")) \
            and not any(a in ("key", "type", "click", "mousemove", "windowkill", "windowclose", "set_window",
                              "exec", "behave") for a in args)
    if cmd == "swaymsg":
        i = args.index("-t") if "-t" in args else -1
        return 0 <= i < len(args) - 1 and args[i + 1].startswith("get_")
    if cmd == "mount":
        return not [a for a in args if not a.startswith("-")] and set(args) <= {"-l", "--show-labels"}
    if cmd == "route":
        return not [a for a in args if a in ("add", "del", "delete", "flush")]
    if cmd in ("tar", "bsdtar"):  # nur auflisten (t / --list), nie entpacken/erstellen
        if "--list" in args:
            return not any(a in ("-x", "-c", "--extract", "--create", "--get") for a in args)
        mode = (args[0].lstrip("-") if args else "")
        return "t" in mode and not set(mode) & set("cxruA")
    if cmd == "unzip":
        return "-l" in args or "-v" in args or "-Z" in args
    if cmd == "7z":
        return bool(args) and args[0] in ("l", "t", "i")
    if cmd == "git":
        sub = next((a for a in args if not a.startswith("-")), "")
        if sub == "branch":
            return all(a in ("-a", "-r", "-v", "-vv", "--list", "--all", "--remotes", "--show-current") or not a.startswith("-")
                       for a in args[1:]) and len([a for a in args[1:] if not a.startswith("-")]) == 0
        if sub in ("remote", "tag", "stash"):
            rest = [a for a in args[1:] if not a.startswith("-")]
            return (sub == "remote" and not rest) or (sub == "tag" and not rest) or (rest[:1] == ["list"])
        return False
    if cmd in ("python3", "python", "node", "java", "go", "rustc", "cargo", "gcc", "clang", "make", "cmake",
               "docker-compose", "kubectl", "ruby", "perl", "php", "deno", "bun"):
        return args in (["--version"], ["-V"], ["-v"], ["version"])
    return cmd in SAFE_COMMANDS


def _reads_secret(targets: list[str], cwd: str, recursive: bool) -> bool:
    """Trifft eines der Ziele (nach cd, ~, $VAR, {a,b} und Globs wie die Shell) eine Geheimnis-Datei – oder
    durchsucht es rekursiv einen Ordner, in dem welche liegen? Unauflösbare Pfade gelten als verdächtig."""
    for arg in targets:
        paths = expand_arg(arg, cwd)
        if paths is None:
            return True
        for p in paths:
            if is_secret_path(p) or (recursive and contains_secrets(p)):
                return True
    return False


def _prints_secrets(name: str, args: list[str], cwd: str) -> bool:
    if name == "printenv":
        return True
    if name == "nmcli":  # -s / --show-secrets zeigt WLAN- und VPN-Passwörter
        return any(a == "--show-secrets" or (a.startswith("-") and not a.startswith("--") and "s" in a)
                   for a in args)
    if name == "ps":  # BSD-Modifikator „e“ (ps eww, ps auxe) gibt die Umgebung jedes Prozesses aus
        return any(a.isalpha() and "e" in a for a in args if not a.startswith("-"))
    if name == "systemctl":  # Units und Manager-Umgebung können Environment=…TOKEN enthalten
        sub = next((a for a in args if not a.startswith("-")), "")
        return sub in ("cat", "show-environment") or (sub == "show" and not any(
            a in ("-p", "--property") or a.startswith("--property=") or (a.startswith("-p") and len(a) > 2)
            for a in args))
    if name not in READERS:
        return False
    targets = [a for a in args if not a.startswith("-") and a not in REDIRECTS and a != "<"]
    recursive = name == "rg" or (name in ("grep", "egrep", "fgrep") and any(
        a in RECURSIVE_FLAGS or (a.startswith("-") and not a.startswith("--") and set(a[1:]) & {"r", "R"})
        for a in args))
    if recursive and len(targets) <= 1:
        targets = [*targets, "."]  # ohne Pfad durchsuchen grep -r und rg das aktuelle Verzeichnis
    return _reads_secret(targets, cwd, recursive)


def classify_command(command: str, cwd: str | None = None) -> tuple[str, str]:
    cmd = command.strip()
    if not cmd:
        return BLOCKED, T("leerer Befehl", "empty command")
    for pattern, reason in BLOCK_PATTERNS:
        if pattern.search(cmd):
            return BLOCKED, reason()
    try:
        tokens = _tokens(cmd)
    except ValueError:
        return CONFIRM, T("Befehl konnte nicht sicher analysiert werden", "the command could not be analysed safely")

    segments = _segments(tokens)
    reasons: list[str] = []
    secrets = T("liest Zugangsdaten (Schlüssel/Passwörter)", "reads credentials (keys/passwords)")
    cwd = cwd or os.path.expanduser("~")  # run_shell startet im Home bzw. im Projektordner
    for seg in segments:
        core, root = _strip_wrappers(seg)
        for i, t in enumerate(seg[:-1]):  # Eingabeumleitung: cat < ~/.ssh/id_rsa
            if t == "<" and _reads_secret([seg[i + 1]], cwd, recursive=False):
                reasons.append(secrets)
        if not core:
            if any(os.path.basename(t) == "env" for t in seg):
                reasons.append(secrets)  # „env“ allein gibt alle Umgebungsvariablen samt Tokens aus
            continue
        name = os.path.basename(core[0])
        args = core[1:]
        if name in ("cd", "pushd"):  # spätere relative Pfade beziehen sich auf den neuen Ordner
            target = next((a for a in args if not a.startswith("-")), "~")
            cwd = os.path.join(cwd, os.path.expanduser(os.path.expandvars(target)))
        if _prints_secrets(name, args, cwd):
            reasons.append(secrets)
        if name == "rm" and _rm_is_catastrophic(args):
            return BLOCKED, T("rekursives Löschen eines Systemverzeichnisses", "recursive deletion of a system directory")
        if root:
            reasons.append(T("benötigt Root-Rechte", "needs root privileges"))
        elif name != "printenv" and not _segment_is_safe(name, [a for a in args if a not in REDIRECTS]):
            reasons.append(T(f"'{name}' kann das System verändern", f"'{name}' can change the system"))

    for i, t in enumerate(tokens):
        if t in REDIRECTS:
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            if target not in ("/dev/null",) and not target.startswith("&"):
                reasons.append(T("schreibt in eine Datei", "writes to a file"))
                break
    if SECRET_VARS.search(cmd) or re.search(r"\benviron\b", _scan_text(cmd)):
        reasons.append(secrets)
    if substitutions(cmd) or "<(" in cmd or ">(" in cmd:
        reasons.append(T("enthält Befehlsersetzung", "contains command substitution"))
    for pattern, reason in WARN_PATTERNS:
        if pattern.search(cmd):
            reasons.append(T("ACHTUNG: ", "WARNING: ") + reason())

    if reasons:
        return CONFIRM, "; ".join(dict.fromkeys(reasons))
    return SAFE, T("nur lesender Befehl", "read-only command")


# Lesende Befehle, die trotzdem ins Netz gehen (DNS-Anfragen können Daten hinaustragen: dig geheim.example.com)
NET_LOOKUP = {"dig", "host", "nslookup", "whois", "traceroute", "tracepath", "mtr", "getent", "ping", "drill"}


def local_read_only(command: str, cwd: str | None = None) -> bool:
    """Rein lesend und lokal (echo, cd, ls, cat …): darf auch nach fremden Inhalten ohne Rückfrage laufen."""
    if classify_command(command, cwd)[0] != SAFE:
        return False
    try:
        tokens = _tokens(command.strip())
    except ValueError:
        return False
    return not any(os.path.basename(t) in NET_LOOKUP for t in tokens)


def file_edit_ok(command: str, cwd: str | None = None) -> bool:
    """Auto-Modus „Dateien bearbeiten“: besteht der Befehl nur aus lesenden Teilen und Dateiänderungen im eigenen
    Home (anlegen, schreiben, kopieren, verschieben) – ohne Root, Löschen, Zugangsdaten oder Befehlsersetzung?"""
    from .filepolicy import shell_edits_ok

    cmd = command.strip()
    if not cmd or "$(" in cmd or "`" in cmd or SECRET_VARS.search(cmd) or re.search(r"\benviron\b", cmd):
        return False
    if any(p.search(cmd) for p, _ in BLOCK_PATTERNS) or any(p.search(cmd) for p, _ in WARN_PATTERNS):
        return False
    try:
        tokens = _tokens(cmd)
    except ValueError:
        return False
    cwd = cwd or os.path.expanduser("~")
    edits: list[tuple[str, list[str], str]] = []
    redirects: list[tuple[str, str]] = []
    for seg in _segments(tokens):
        core, root = _strip_wrappers(seg)
        if root:
            return False
        for i, t in enumerate(seg[:-1]):
            if t == "<" and _reads_secret([seg[i + 1]], cwd, recursive=False):
                return False
        if not core:
            if any(os.path.basename(t) == "env" for t in seg):
                return False
            continue
        args, skip = [], False
        for i, t in enumerate(core[1:], 1):
            if skip:
                skip = False
                continue
            if t in REDIRECTS:
                redirects.append((core[i + 1] if i + 1 < len(core) else "", cwd))
                skip = True
                continue
            args.append(t)
        name = os.path.basename(core[0])
        if name in ("cd", "pushd"):
            target = next((a for a in args if not a.startswith("-")), "~")
            cwd = os.path.join(cwd, os.path.expanduser(os.path.expandvars(target)))
            continue
        if _prints_secrets(name, args, cwd):
            return False
        if not _segment_is_safe(name, args):
            edits.append((name, args, cwd))
    return bool(edits or redirects) and shell_edits_ok(edits, redirects)


# ---------------------------------------------------------------- Auto-Modus „Auto“ (alles ohne Root)
_ROOT_WORD = re.compile(r"(?<![\w./-])(sudo|su|pkexec|doas|run0)(?![\w.-])")
DELETE_CMDS = {"rm", "rmdir", "unlink", "shred", "trash", "trash-put", "srm", "wipe"}
POWER_CMDS = {"shutdown", "reboot", "poweroff", "halt"}
SEND_CMDS = {"ssh", "scp", "sftp", "mail", "mailx", "sendmail", "mutt", "nc", "ncat", "telnet", "ftp"}
SHELLS = {"bash", "sh", "zsh", "dash", "fish"}
_SEND_FLAGS = re.compile(r"^(-d\S*|--data\S*|-F\S*|--form\S*|-T\S*|--upload-file|--post-data|--post-file|"
                         r"--body-data|--body-file|--method)(=.*)?$", re.I)


def _git_sub(args: list[str]) -> str:
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-C", "-c", "--git-dir", "--work-tree"):
            i += 2
            continue
        if not a.startswith("-"):
            return a
        i += 1
    return ""


def _write_targets(name: str, args: list[str]) -> list[str]:
    """Ziele, in die ein Befehl schreibt (für den Schutz von Startdateien, Autostart, ~/.ssh …)."""
    paths = [a for a in args if not a.startswith("-")]
    if name == "tee":
        return paths
    if name in ("cp", "mv", "install", "ln", "rsync"):
        for i, a in enumerate(args):
            if a in ("-t", "--target-directory") and i + 1 < len(args):
                return [args[i + 1]]
            if a.startswith("--target-directory="):
                return [a.split("=", 1)[1]]
        return paths[-1:] if len(paths) >= 2 else []
    if name in ("touch", "mkdir"):
        return paths
    return []


def substitutions(cmd: str) -> list[str]:
    """Inhalt von $(…), `…` (auch in doppelten Anführungszeichen – dort wird es ebenfalls ausgeführt) und <(…)/>(…)."""
    out, i, quote = [], 0, ""
    while i < len(cmd):
        c = cmd[i]
        if c == "\\":
            i += 2
            continue
        if quote == "'":
            quote = "" if c == "'" else quote
        elif c == "'" and not quote:
            quote = "'"
        elif c == '"':
            quote = "" if quote == '"' else '"'
        elif (cmd.startswith("$(", i) and not cmd.startswith("$((", i)) or (not quote and cmd[i:i + 2] in ("<(", ">(")):
            depth, j = 1, i + 2
            while j < len(cmd) and depth:
                depth += {"(": 1, ")": -1}.get(cmd[j], 0)
                j += 1
            out.append(cmd[i + 2:j - 1])
            i = j
            continue
        elif c == "`":
            j = cmd.find("`", i + 1)
            j = len(cmd) if j < 0 else j
            out.append(cmd[i + 1:j])
            i = j + 1
            continue
        i += 1
    return out


# echo/printf mit reinem Text: Wörter darin (sudo, environ …) sind Text, kein Befehl
_TEXT_ARGS = re.compile(r"""(?<![\w./-])(echo|printf)((?:[ \t]+(?:'[^']*'|"[^"$`\\]*"|-[A-Za-z]+))*)(?=[ \t]*(?:$|[;&|>\n)]))""",
                        re.M)


# Führen Text als Befehl aus (echo "sudo …" | bash, bash <<EOF): dann bleibt der Text Teil der Prüfung
_RUNS_TEXT = {"eval", "xargs", "watch", "source", ".", "python", "python3", "perl", "ruby", "node", "php", "lua",
              "awk", "gawk", "sed", "su", "script", "flock", "parallel", "tmux", "screen", "systemd-run", "at", "batch"}


def _scan_text(cmd: str) -> str:
    """Der Befehl ohne Heredoc-Text und ohne reinen Text in echo/printf – für die Suche nach Schlüsselwörtern."""
    try:
        names = {os.path.basename(t) for t in _tokens(cmd)}
    except ValueError:
        return cmd
    if names & (SHELLS | _RUNS_TEXT):
        return cmd
    return _TEXT_ARGS.sub(lambda m: m.group(1), strip_heredocs(cmd))


def auto_shell_ok(command: str, cwd: str | None = None, _depth: int = 0) -> tuple[bool, str]:
    """Auto-Modus „Auto“: läuft dieser Befehl ohne Rückfrage? Alles ohne Root – außer Löschen, Ausschalten,
    Senden ins Netz, Startdateien/Autostart/Zugangsdaten. Liefert (ok, Grund fürs Nachfragen)."""
    from .filepolicy import protected_path

    still = T(" – fragt auch im Auto-Modus", " – Auto still asks")
    root = T("benötigt Root-Rechte", "needs root privileges") + still
    secrets = T("liest Zugangsdaten (Schlüssel/Passwörter)", "reads credentials (keys/passwords)") + still
    delete = T("löscht Dateien", "deletes files") + still
    power = T("schaltet aus, startet neu oder beendet die Sitzung", "shuts down, reboots or ends the session") + still
    send = T("sendet etwas ins Netz", "sends something over the network") + still
    persist = T("ändert Startdateien, Autostart, Zugangsdaten oder Orbwise selbst",
                "changes start-up files, autostart, credentials or Orbwise itself") + still
    cmd = command.strip()
    if not cmd or _depth > 3:
        return False, T("Befehl konnte nicht sicher analysiert werden", "the command could not be analysed safely")
    for pattern, reason in BLOCK_PATTERNS:
        if pattern.search(cmd):
            return False, reason() + still
    scan = _scan_text(cmd)
    for pattern, reason in WARN_PATTERNS:
        if pattern.search(scan):
            return False, reason() + still
    if _ROOT_WORD.search(scan):
        return False, root
    if SECRET_VARS.search(cmd) or re.search(r"\benviron\b", scan):
        return False, secrets
    for inner in substitutions(strip_heredocs(cmd)):  # echo "$(rm -rf ~)" führt rm aus
        ok, why = auto_shell_ok(inner, cwd, _depth + 1) if inner.strip() else (True, "")
        if not ok:
            return ok, why
    try:
        tokens = _tokens(cmd)
    except ValueError:
        return False, T("Befehl konnte nicht sicher analysiert werden", "the command could not be analysed safely")
    cwd = cwd or os.path.expanduser("~")
    for seg in _segments(tokens):
        core, is_root = _strip_wrappers(seg)
        if is_root:
            return False, root
        for i, t in enumerate(seg[:-1]):
            if t == "<" and _reads_secret([seg[i + 1]], cwd, recursive=False):
                return False, secrets
        if not core:
            if any(os.path.basename(t) == "env" for t in seg):
                return False, secrets
            continue
        args, skip = [], False
        for i, t in enumerate(core[1:], 1):
            if skip:
                skip = False
                continue
            if t in REDIRECTS:
                target = core[i + 1] if i + 1 < len(core) else ""
                if target != "/dev/null" and not target.startswith("&") and protected_path(target, cwd):
                    return False, persist
                skip = True
                continue
            args.append(t)
        name = os.path.basename(core[0])
        if name in ("cd", "pushd"):
            target = next((a for a in args if not a.startswith("-")), "~")
            cwd = os.path.join(cwd, os.path.expanduser(os.path.expandvars(target)))
            continue
        if _prints_secrets(name, args, cwd):
            return False, secrets
        if name in SHELLS and "-c" in args:  # bash -c "…": den inneren Befehl genauso prüfen
            inner = args[args.index("-c") + 1] if args.index("-c") + 1 < len(args) else ""
            ok, why = auto_shell_ok(inner, cwd, _depth + 1)
            if not ok:
                return ok, why
        if name == "eval":
            ok, why = auto_shell_ok(" ".join(args), cwd, _depth + 1)
            if not ok:
                return ok, why
        if name == "xargs":  # xargs rm …
            sub = next((a for a in args if not a.startswith("-")), "")
            if os.path.basename(sub) in DELETE_CMDS | POWER_CMDS | SEND_CMDS:
                return False, delete if os.path.basename(sub) in DELETE_CMDS else send
        if name in DELETE_CMDS or (name == "gio" and args[:1] in (["trash"], ["remove"])):
            return False, delete
        if name == "find" and ("-delete" in args or any(
                a in ("-exec", "-execdir", "-ok", "-okdir") and i + 1 < len(args)
                and os.path.basename(args[i + 1]) in DELETE_CMDS for i, a in enumerate(args))):
            return False, delete
        if name == "git":
            sub = _git_sub(args)
            if sub == "push":
                return False, send
            if sub == "clean":
                return False, delete
        if name in POWER_CMDS:
            return False, power
        if name == "systemctl":
            verbs = [a for a in args if not a.startswith("-")]
            if verbs[:1] and verbs[0] in ("poweroff", "reboot", "suspend", "hibernate", "halt", "hybrid-sleep",
                                          "suspend-then-hibernate", "kexec", "soft-reboot"):
                return False, power
            if verbs[:1] and verbs[0] in ("enable", "link", "preset", "preset-all", "set-environment"):
                return False, persist
        if name == "loginctl" and any(a.startswith(("terminate", "kill", "poweroff", "reboot")) for a in args):
            return False, power
        if name == "crontab" and "-l" not in args:
            return False, persist
        if name in SEND_CMDS:
            return False, send
        if name == "rsync" and any(re.match(r"^[\w.@-]+:", a) and not a.startswith(("/", "~", "."))
                                   for a in args if not a.startswith("-")):
            return False, send
        if name in ("curl", "wget"):
            if any(_SEND_FLAGS.match(a) for a in args):
                return False, send
            for i, a in enumerate(args):
                method = args[i + 1] if a in ("-X", "--request") and i + 1 < len(args) else (
                    a[2:] if a.startswith("-X") and len(a) > 2 else "")
                if method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
                    return False, send
        if any(protected_path(t, cwd) for t in _write_targets(name, args)):
            return False, persist
    return True, ""


_SUDO_RE = re.compile(r"(^|[;&|(]\s*|\s)sudo((?:\s+(?:-[ugpCrtUDRTh]\s+[^\s-]\S*|-[A-Za-z]+))*)\s+")
# Optionen mit Wert (z. B. -u root) bleiben erhalten; -n/-S/-A werden durch den gewählten Modus ersetzt
_SUDO_OWN_FLAGS = re.compile(r"\s+-[nSA]+\b")


_PKEXEC_RE = re.compile(r"(^|[;&|(]\s*|\s)pkexec\s+")


def apply_privilege(command: str, privilege_cmd: str) -> str:
    """Ersetzt 'sudo' durch das konfigurierte Werkzeug: 'orbwise' → sudo -A (Passwortdialog in der Oberfläche),
    'pkexec' → grafischer Polkit-Dialog, 'sudo' → sudo -n (nur mit NOPASSWD-Regel)."""
    if privilege_cmd in ("dashboard", "orbwise", "jarvis"):
        command = _PKEXEC_RE.sub(lambda m: f"{m.group(1)}sudo ", command)
        return _SUDO_RE.sub(lambda m: f"{m.group(1)}sudo -A{_SUDO_OWN_FLAGS.sub('', m.group(2))} ", command)
    if privilege_cmd == "pkexec":
        return _SUDO_RE.sub(lambda m: f"{m.group(1)}pkexec ", command)
    if privilege_cmd == "sudo":
        return _SUDO_RE.sub(lambda m: f"{m.group(1)}sudo -n ", command)
    return command
