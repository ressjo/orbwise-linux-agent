"""Welche Programme mit welchen Argumenten nur lesen. Ein Programm, das hier nicht als lesend gilt, braucht eine
Rückfrage (bzw. wird im Auto-Modus gesondert bewertet, siehe auto.py).

Reihenfolge der Prüfung (is_read_only): eigene Regel des Programms (RULES) → Unterbefehle (SAFE_SUBCOMMANDS) → nur
bestimmte Optionen (ONLY_FLAGS) → verbotene Optionen (FORBIDDEN_FLAGS) → nur Versionsabfrage (VERSION_ONLY) →
immer lesend (SAFE_COMMANDS)."""

from __future__ import annotations

import re
from collections.abc import Callable

from .shellwords import AWKS, first_word, operands, words

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

# Befehle mit Unterbefehlen: nur diese (lesenden) Unterbefehle gelten als sicher; "" = ohne Unterbefehl
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
# Unterbefehl erlaubt – aber diese Wörter dahinter nicht (docker volume rm, iw dev wlan0 set …)
_CONTAINER_CHANGES = {"rm", "rmi", "prune", "create", "run", "exec", "kill", "stop", "start", "restart", "up", "down",
                      "pull", "push", "build", "pause", "unpause", "update", "connect", "disconnect", "cp", "commit",
                      "tag", "load", "save", "import"}
_IW_CHANGES = {"set", "connect", "disconnect", "del", "add", "ibss", "mesh", "join", "leave", "interface", "switch",
               "offchannel", "vendor", "wowlan", "coalesce"}

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
# Ohne Option zählen alle Argumente (xset q); ohne Argument ist es keine Abfrage
_ONLY_FLAGS_ALL_ARGS = {"xset"}
_ONLY_FLAGS_NEED_ONE = {"hdparm", "setxkbmap", "xset", "arecord", "aplay", "v4l2-ctl"}

# Nur die Versionsabfrage ist lesend (python3 --version); kubectl hat eigene Unterbefehle (SAFE_SUBCOMMANDS)
VERSION_ONLY = {"python3", "python", "node", "java", "go", "rustc", "cargo", "gcc", "clang", "make", "cmake",
                "docker-compose", "ruby", "perl", "php", "deno", "bun"}
_VERSION_ARGS = (["--version"], ["-V"], ["-v"], ["version"])


# ---------------------------------------------------------------- Regeln je Programm

_AWK_RISK = re.compile(r"\bsystem\s*\(|\bgetline\b|\||\b(print|printf)\b[^;{}]*>")


def _find(args: list[str]) -> bool:
    return not any(a in ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls") for a in args)


def _sed(args: list[str]) -> bool:
    return not any(a.startswith("-i") or a.startswith("--in-place") for a in args)


def _awk(name: str) -> Callable[[list[str]], bool]:
    """awk ist nur gefährlich mit system(), Pipes zu Befehlen, Ausgabe in Dateien oder Programmdateien."""
    def rule(args: list[str]) -> bool:
        if any(a in ("-f", "--file", "-i", "--include") or a.startswith(("--file=", "--include=", "-f", "-i"))
               for a in args if a.startswith("-")):
            return False
        program = next(iter(operands(name, args, programs=True)), "")
        return not _AWK_RISK.search(program)
    return rule


def _top(args: list[str]) -> bool:
    return "-b" in args or any(a.startswith("-b") for a in args)


def _ping(args: list[str]) -> bool:
    return any(a.startswith("-c") for a in args)


def _pacman(args: list[str]) -> bool:
    op = next((a for a in args if a.startswith("-")), "")
    if op.startswith("-Q") or op.startswith("--query"):
        return True
    if op.startswith("-S") and len(op) > 2 and set(op[2:]) <= set("silg"):
        return True
    if op.startswith("-F") and "y" not in op:
        return True
    return op in ("--version", "-V")


_SYSTEMCTL_READ = {"status", "list-units", "list-unit-files", "list-timers", "is-active", "is-enabled", "is-failed",
                   "show", "cat", "list-dependencies", "list-sockets", "list-jobs", "list-machines", "get-default",
                   "is-system-running"}


def _systemctl(args: list[str]) -> bool:
    sub = first_word(args)
    return sub in _SYSTEMCTL_READ or (not sub and "--failed" in args)


def _ip(args: list[str]) -> bool:
    w = words(args)
    return bool(w) and w[0] in ("a", "addr", "address", "l", "link", "r", "route", "n", "neigh") \
        and not any(x in ("add", "del", "delete", "set", "flush", "change", "replace") for x in w[1:])


_GIT_READ = {"status", "log", "diff", "show", "ls-files", "blame", "rev-parse", "shortlog", "describe", "reflog",
             "ls-remote", "grep", "whatchanged", "cat-file", "ls-tree", "count-objects"}
_GIT_BRANCH_LIST = {"-a", "-r", "-v", "-vv", "--list", "--all", "--remotes", "--show-current"}


def _git(args: list[str]) -> bool:
    sub = first_word(args)
    if sub in _GIT_READ:
        return True
    if sub == "branch":  # nur auflisten
        return all(a in _GIT_BRANCH_LIST or not a.startswith("-") for a in args[1:]) and not words(args[1:])
    if sub in ("remote", "tag", "stash"):
        rest = words(args[1:])
        return (sub == "remote" and not rest) or (sub == "tag" and not rest) or (rest[:1] == ["list"])
    return False


def _flatpak(args: list[str]) -> bool:
    return first_word(args) in ("list", "info", "search", "find")


def _dpkg(args: list[str]) -> bool:
    return any(a in ("-l", "--list", "-L", "--listfiles", "-s", "--status", "-S", "--search", "-p", "--print-avail",
                     "--get-selections", "--print-architecture") for a in args)


def _rpm(args: list[str]) -> bool:
    op = next((a for a in args if a.startswith("-")), "")
    return op.startswith("-q") or op in ("--query", "-V", "--verify")


def _journalctl(args: list[str]) -> bool:
    return not any(a.startswith("--vacuum") or a in ("--rotate", "--flush") for a in args)


def _show_only(args: list[str]) -> bool:  # pactl, wpctl, timedatectl …: nur anzeigen
    return first_word(args) in ("", "status", "list", "info", "show", "get", "inspect", "get-volume", "scontents")


def _nmcli(args: list[str]) -> bool:
    """nmcli [Objekt] [Verb]: nur anzeigen (device status, connection show, general, radio …)."""
    w = words(args)
    obj = w[0] if w else ""
    verb = w[1] if len(w) > 1 else ""
    if obj in ("", "g", "ge", "general", "n", "networking", "r", "radio", "m", "monitor"):
        return verb in ("", "status", "permissions", "hostname", "logging", "connectivity", "all", "wifi", "wwan") \
            and len(w) <= 2
    if obj in ("d", "dev", "device", "c", "con", "connection"):
        if verb == "wifi":
            return len(w) <= 3 and (w[2] if len(w) > 2 else "list") in ("list", "show-password") \
                and "show-password" not in w
        return verb in ("", "status", "show", "s", "list", "monitor")
    return False


def _wmctrl(args: list[str]) -> bool:  # -l/-d/-m listen nur; -c/-k/-r … ändern Fenster
    return bool(args) and all(a in ("-l", "-d", "-m", "-p", "-G", "-x", "-lp", "-lx", "-lG") for a in args)


def _xdotool(args: list[str]) -> bool:
    return bool(args) and (args[0].startswith("get") or args[0] in ("search", "version")) \
        and not any(a in ("key", "type", "click", "mousemove", "windowkill", "windowclose", "set_window", "exec",
                          "behave") for a in args)


def _swaymsg(args: list[str]) -> bool:
    i = args.index("-t") if "-t" in args else -1
    return 0 <= i < len(args) - 1 and args[i + 1].startswith("get_")


def _mount(args: list[str]) -> bool:
    return not words(args) and set(args) <= {"-l", "--show-labels"}


def _route(args: list[str]) -> bool:
    return not [a for a in args if a in ("add", "del", "delete", "flush")]


def _tar(args: list[str]) -> bool:  # nur auflisten (t / --list), nie entpacken/erstellen
    if "--list" in args:
        return not any(a in ("-x", "-c", "--extract", "--create", "--get") for a in args)
    mode = (args[0].lstrip("-") if args else "")
    return "t" in mode and not set(mode) & set("cxruA")


def _unzip(args: list[str]) -> bool:
    return "-l" in args or "-v" in args or "-Z" in args


def _7z(args: list[str]) -> bool:
    return bool(args) and args[0] in ("l", "t", "i")


RULES: dict[str, Callable[[list[str]], bool]] = {
    "find": _find, "sed": _sed, **{a: _awk(a) for a in AWKS}, "top": _top, "ping": _ping,
    **dict.fromkeys(("pacman", "yay", "paru"), _pacman), "systemctl": _systemctl, "ip": _ip, "git": _git,
    "flatpak": _flatpak, "snap": _flatpak, "dpkg": _dpkg, "rpm": _rpm, "journalctl": _journalctl,
    **dict.fromkeys(("pactl", "wpctl", "amixer", "xrandr", "timedatectl", "hostnamectl", "loginctl"), _show_only),
    "nmcli": _nmcli, "wmctrl": _wmctrl, "xdotool": _xdotool, "swaymsg": _swaymsg, "mount": _mount, "route": _route,
    "tar": _tar, "bsdtar": _tar, "unzip": _unzip, "7z": _7z,
}
assert not set(RULES) & (set(SAFE_SUBCOMMANDS) | set(ONLY_FLAGS) | set(FORBIDDEN_FLAGS) | VERSION_ONLY)


# ---------------------------------------------------------------- Tabellen

def _subcommands(cmd: str, args: list[str]) -> bool:
    allowed = SAFE_SUBCOMMANDS[cmd]
    sub = first_word(args)
    if sub not in allowed and not (sub == "" and "" in allowed):
        return sub == "" and any(a in ("--version", "-v", "-V", "--help") for a in args)
    w = words(args)
    if cmd in ("docker", "podman"):  # z. B. docker volume ls, docker compose ps – keine rm/prune/up …
        return not any(x in _CONTAINER_CHANGES for x in w[1:])
    if cmd == "gsettings":
        return "set" not in w and "reset" not in w
    if cmd == "iw":  # iw dev / iw dev wlan0 link / iw list
        return not any(x in _IW_CHANGES for x in w)
    if cmd == "wg":
        return len(w) <= 2 and not any(x in ("private-key", "preshared-keys") for x in w)
    return True


def _only_flags(cmd: str, args: list[str]) -> bool:
    """Geräte/Pfade als Argument erlaubt, Optionen nur aus der Liste."""
    flags = [a for a in args if a.startswith("-") or cmd in _ONLY_FLAGS_ALL_ARGS]
    return all(a in ONLY_FLAGS[cmd] for a in flags) and (cmd not in _ONLY_FLAGS_NEED_ONE or bool(flags))


def _forbidden_flags(cmd: str, args: list[str]) -> bool:
    bad = FORBIDDEN_FLAGS[cmd]
    return not any(a in bad or any(a.startswith(f + "=") for f in bad) for a in args)


def is_read_only(cmd: str, args: list[str]) -> bool:
    """Liest dieser Aufruf nur (args ohne Umleitungsoperatoren)?"""
    if cmd in RULES:
        return RULES[cmd](args)
    if cmd in SAFE_SUBCOMMANDS:
        return _subcommands(cmd, args)
    if cmd in ONLY_FLAGS:
        return _only_flags(cmd, args)
    if cmd in FORBIDDEN_FLAGS:
        return _forbidden_flags(cmd, args)
    if cmd in VERSION_ONLY:
        return args in _VERSION_ARGS
    return cmd in SAFE_COMMANDS
