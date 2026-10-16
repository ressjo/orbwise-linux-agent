"""Datenabfluss-Wächter: Was ein Werkzeug ins Internet schickt (Suchanfrage, URL, curl/wget), wird vorher auf private
Daten geprüft – Zugangsdaten, IBAN/Kartennummern, E-Mail-Adressen, Telefonnummern, lange kodierte Parameter und
wörtliche Inhalte aus eigenen Dateien/Diensten. Bei einem Treffer fragt Orbwise nach, statt still zu senden.

Steht der Treffer wörtlich in der letzten Nachricht des Nutzers, hat er es selbst so gewollt – dann keine Rückfrage.
"""

from __future__ import annotations

import re
import shlex
from urllib.parse import parse_qsl, unquote_plus, urlparse

from ..lang import T

# Werkzeuge, deren Ergebnisse lokale Daten enthalten (Dateien, Mails, Dokumente, Gedächtnis, andere Rechner) – ihr
# Inhalt darf nicht unbemerkt in einer URL landen. Web-Ergebnisse zählen nicht (die kommen ja schon von dort).
LOCAL_DATA_TOOLS = {"read_file", "search_file_contents", "find_files", "list_directory", "search_nas", "run_shell",
                    "ssh_run", "mail_read", "mail_ask", "mail_list", "mail_search", "paperless_read", "paperless_ask",
                    "paperless_search", "obsidian_read", "obsidian_search", "trilium_read", "trilium_search",
                    "memory_search", "recall", "calendar_events", "system_info", "portainer_containers"}
NET_COMMANDS = {"curl", "wget", "http", "https", "xh", "httpie", "aria2c", "lynx", "w3m", "links"}

_TOKENS = [
    (re.compile(r"\b(sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|"
                r"xox[abposr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|glpat-[A-Za-z0-9_-]{20,})"),
     lambda: T("einen Zugangsschlüssel (API-Token)", "an access key (API token)")),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"),
     lambda: T("ein Anmelde-Token (JWT)", "a login token (JWT)")),
    (re.compile(r"-----BEGIN [A-Z ]*(PRIVATE KEY|CERTIFICATE)-----"),
     lambda: T("einen privaten Schlüssel", "a private key")),
    (re.compile(r"(?i)\b(pass(?:word|wd)?|pwd|passwort|kennwort|token|api[_-]?key|secret|access[_-]?key|auth)"
                r"\s*[=:]\s*[^\s&]{3,}"),
     lambda: T("ein Passwort oder einen Schlüssel", "a password or key")),
]
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}\b")
_PHONE = re.compile(r"(?<![\w.])(?:\+\d{1,3}[\s/-]?|00\d{2}[\s/-]?|0)\d{2,5}(?:[\s/-]?\d{2,}){1,4}(?![\w.])")
_IBAN = re.compile(r"\b([A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,3})?)\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_LONG_RUN = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
WINDOW = 40  # so viele Zeichen am Stück aus einem lokalen Ergebnis gelten als „Inhalt einer Datei“


def _iban_ok(text: str) -> bool:
    s = text.replace(" ", "")
    if not 15 <= len(s) <= 34:
        return False
    digits = "".join(str(int(c, 36)) for c in s[4:] + s[:4])
    return int(digits) % 97 == 1


def _luhn_ok(text: str) -> bool:
    digits = [int(c) for c in re.sub(r"\D", "", text)]
    if not 13 <= len(digits) <= 19 or len(set(digits)) < 3:
        return False
    total = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2:
            d *= 2
            d -= 9 if d > 9 else 0
        total += d
    return total % 10 == 0


def mask(value: str) -> str:
    value = value.strip()
    return value if len(value) <= 8 else f"{value[:4]}…{value[-4:]}"


def outbound_text(name: str, args: dict) -> str:
    """Der Teil eines Aufrufs, der den Rechner verlässt (URL-dekodiert) – leer, wenn nichts hinausgeht."""
    parts: list[str] = []
    if name == "web_search":
        parts.append(str(args.get("query") or ""))
    elif name == "fetch_url":
        parts.append(str(args.get("url") or ""))
    elif name == "open_website":
        parts += [str(args.get("site") or ""), str(args.get("query") or "")]
    elif name in ("run_shell", "ssh_run"):
        command = str(args.get("command") or "")
        try:
            words = shlex.split(command)
        except ValueError:
            words = command.split()
        names = {w.rsplit("/", 1)[-1] for w in words}
        if names & NET_COMMANDS:
            for i, w in enumerate(words):
                if re.match(r"^(https?|ftp)://", w):
                    parts.append(w)
                elif w in ("-d", "--data", "--data-raw", "--data-urlencode", "--data-binary", "--post-data", "-F",
                           "--form", "-H", "--header", "-u", "--user") and i + 1 < len(words):
                    parts.append(words[i + 1])
    text = " ".join(p for p in parts if p)
    return unquote_plus(text) if text else ""


def _params(text: str) -> list[str]:
    values = []
    for word in text.split():
        if "://" in word:
            values += [v for _, v in parse_qsl(urlparse(word).query, keep_blank_values=False)]
            values += [seg for seg in urlparse(word).path.split("/") if seg]
    return values


def find_leak(text: str, user_text: str = "", local_texts: list[str] | None = None) -> str | None:
    """Grund, falls der ausgehende Text private Daten enthält (mit gekürztem Treffer) – sonst None."""
    if not text:
        return None
    user = user_text or ""

    def hit(value: str, what: str) -> str | None:
        if value.strip() and value.strip().lower() in user.lower():
            return None  # vom Nutzer selbst genannt
        return T(f"würde {what} ins Internet senden ({mask(value)})", f"would send {what} to the internet ({mask(value)})")

    for pattern, what in _TOKENS:
        for m in pattern.finditer(text):
            if (r := hit(m.group(0), what())):
                return r
    for m in _IBAN.finditer(text.upper()):
        if _iban_ok(m.group(1)) and (r := hit(m.group(1), T("eine IBAN", "an IBAN"))):
            return r
    for m in _CARD.finditer(text):
        if _luhn_ok(m.group(0)) and (r := hit(m.group(0), T("eine Kartennummer", "a card number"))):
            return r
    for m in _EMAIL.finditer(text):
        if (r := hit(m.group(0), T("eine E-Mail-Adresse", "an e-mail address"))):
            return r
    for m in _PHONE.finditer(text):
        if len(re.sub(r"\D", "", m.group(0))) >= 9 and (r := hit(m.group(0), T("eine Telefonnummer", "a phone number"))):
            return r
    for value in _params(text):
        if len(value) > 200 and (r := hit(value, T("lange kodierte Daten", "long encoded data"))):
            return r
        for m in _LONG_RUN.finditer(value):
            if (re.search(r"\d", m.group(0)) and re.search(r"[A-Za-z]", m.group(0))
                    and (r := hit(m.group(0), T("einen langen Schlüssel/Code", "a long key/code")))):
                return r
    flat = " ".join(text.split())
    if local_texts and len(flat) >= WINDOW:
        sources = [" ".join(t.split()) for t in local_texts if t]
        for start in range(0, len(flat) - WINDOW + 1, WINDOW // 2):
            piece = flat[start:start + WINDOW]
            if piece.strip() and piece.lower() not in user.lower() and any(piece in src for src in sources):
                return T(f"würde Inhalt aus deinen Dateien/Diensten ins Internet senden („{piece[:24]}…“)",
                         f"would send content of your files/services to the internet (“{piece[:24]}…”)")
    return None
