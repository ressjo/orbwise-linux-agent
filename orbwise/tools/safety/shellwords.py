"""Shell-Befehle zerlegen: Heredocs, Wörter und Operatoren, Teilbefehle, Wrapper (sudo, env, nice …), Befehlsersetzung
und ein gemeinsamer Durchlauf über die Teilbefehle (walk) für alle Prüfungen."""

from __future__ import annotations

import os
import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass, field

SEPARATORS = {";", "&&", "||", "|", "&", "\n", "|&", ";;", "(", ")"}
REDIRECTS = {">", ">>", ">|", "&>", "&>>"}
ROOT_WRAPPERS = {"sudo", "doas", "pkexec", "su", "run0"}
TRANSPARENT_WRAPPERS = {"nice", "time", "command", "nohup", "ionice", "stdbuf"}
SHELLS = {"bash", "sh", "zsh", "dash", "fish"}
AWKS = {"awk", "gawk", "mawk", "nawk"}

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


def tokens(cmd: str) -> list[str]:
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


def segments(toks: list[str]) -> list[list[str]]:
    segs, cur = [], []
    for t in toks:
        if t in SEPARATORS:
            if cur:
                segs.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        segs.append(cur)
    return segs


def strip_wrappers(seg: list[str]) -> tuple[list[str], bool]:
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


@dataclass
class Segment:
    """Ein Teilbefehl. args: ohne Umleitungen (> datei); raw_args: wie geschrieben (Umleitungen samt Ziel inklusive);
    inputs: Dateien hinter „<“ (im ganzen Teilbefehl, auch vor den Wrappern)."""
    raw: list[str]
    core: list[str]
    root: bool
    name: str = ""
    raw_args: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)
    redirects: list[str] = field(default_factory=list)
    inputs: list[str] = field(default_factory=list)

    @property
    def bare_env(self) -> bool:
        """Nur Wrapper, kein Programm – und darunter „env“: gibt alle Umgebungsvariablen samt Tokens aus."""
        return not self.core and any(os.path.basename(t) == "env" for t in self.raw)


def walk(toks: list[str]) -> Iterator[Segment]:
    """Alle Teilbefehle eines zerlegten Befehls, aufbereitet für die Prüfungen."""
    for seg in segments(toks):
        core, root = strip_wrappers(seg)
        s = Segment(raw=seg, core=core, root=root,
                    inputs=[seg[i + 1] for i, t in enumerate(seg[:-1]) if t == "<"])
        if core:
            s.name, s.raw_args = os.path.basename(core[0]), core[1:]
            skip = False
            for i, t in enumerate(core[1:], 1):
                if skip:
                    skip = False
                elif t in REDIRECTS:
                    s.redirects.append(core[i + 1] if i + 1 < len(core) else "")
                    skip = True
                else:
                    s.args.append(t)
        yield s


def changed_dir(cwd: str, args: list[str]) -> str:
    """Ordner nach cd/pushd – spätere relative Pfade beziehen sich darauf."""
    target = next((a for a in args if not a.startswith("-")), "~")
    return os.path.join(cwd, os.path.expanduser(os.path.expandvars(target)))


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


def scan_text(cmd: str) -> str:
    """Der Befehl ohne Heredoc-Text und ohne reinen Text in echo/printf – für die Suche nach Schlüsselwörtern."""
    try:
        names = {os.path.basename(t) for t in tokens(cmd)}
    except ValueError:
        return cmd
    if names & (SHELLS | _RUNS_TEXT):
        return cmd
    return _TEXT_ARGS.sub(lambda m: m.group(1), strip_heredocs(cmd))


# Verdeckte Schreibweisen für die Schlüsselwort-Suche auflösen: /usr/bin/sudo, ./su, ~/bin/git → sudo, su, git;
# s""udo, s\udo → sudo (die Shell macht daraus dasselbe Programm)
_PATH_PREFIX = re.compile(r"(?<![\w.-])(?:~|\.{1,2})?/(?:[\w.+-]+/)*(?=[\w.+-])")
_EMPTY_QUOTES = re.compile(r"""(?<=\w)(?:''|"")|(?:''|"")(?=\w)""")
_WORD_ESCAPE = re.compile(r"\\(?=\w)")


def plain_words(text: str) -> str:
    return _PATH_PREFIX.sub("", _WORD_ESCAPE.sub("", _EMPTY_QUOTES.sub("", text)))


# Optionen mit Wert: der Wert ist kein Datei-Argument
_VALUE_OPTS = {
    "awk": {"-F", "-v", "-f", "--field-separator", "--assign", "--file"},
    "grep": {"-e", "-f", "-m", "-A", "-B", "-C", "--regexp", "--file", "--max-count", "--after-context",
             "--before-context", "--context", "--label", "--color", "--colour", "-d", "-D"},
    "rg": {"-e", "-f", "-m", "-A", "-B", "-C", "-g", "-t", "-T", "--regexp", "--file", "--max-count", "--glob",
           "--type", "--type-not", "-M", "--max-columns", "--max-depth", "-j", "--threads"},
    "sed": {"-e", "-f", "--expression", "--file", "-l", "--line-length"},
}
_PATTERN_OPTS = {"-e", "-f", "--regexp", "--file", "--expression"}


def operands(name: str, args: list[str], programs: bool = False) -> list[str]:
    """Freie Argumente ohne Optionswerte. Bei awk/grep/rg/sed ist das erste davon Programm bzw. Suchmuster – außer es
    kommt über -e/-f. programs=True: nur dieses Programm/Muster (leer, wenn es per -e/-f kommt), sonst die Dateien."""
    kind = "awk" if name in AWKS else "grep" if name in ("grep", "egrep", "fgrep") else name
    value_opts = _VALUE_OPTS.get(kind, set())
    free, from_opt, skip = [], False, False
    for a in args:
        if skip:
            skip = False
            continue
        if a.startswith("-") and a != "-":
            opt = a.split("=", 1)[0]
            short = a[:2] if not a.startswith("--") else opt
            if opt in _PATTERN_OPTS or (short in ("-e", "-f") and kind in ("grep", "rg", "sed", "awk")):
                from_opt = from_opt or not (kind == "awk" and short == "-e")
            if (opt in value_opts and "=" not in a) or (not a.startswith("--") and len(a) == 2 and a in value_opts):
                skip = True
            continue
        free.append(a)
    if kind not in ("awk", "grep", "rg", "sed"):
        return [] if programs else free
    if from_opt:
        return [] if programs else free
    return free[:1] if programs else free[1:]


def first_word(args: list[str]) -> str:
    """Erstes Argument, das keine Option ist (meist der Unterbefehl)."""
    return next((a for a in args if not a.startswith("-")), "")


def words(args: list[str]) -> list[str]:
    return [a for a in args if not a.startswith("-")]
