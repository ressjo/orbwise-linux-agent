"""Root-Rechte per Passwortdialog in der Orbwise-Oberfläche (sudo -A mit eigenem Askpass-Helfer).

Ablauf: Ein Tool führt `sudo -A …` aus. sudo startet den Askpass-Helfer (kleines Skript, von Orbwise beim Start
angelegt). Der Helfer fragt per HTTP mit einem Einmal-Token beim Orbwise-Server nach, der Server zeigt in der
Oberfläche ein Passwortfeld und reicht die Eingabe an den Helfer zurück, der sie an sudo weitergibt.

Das Passwort wird nie auf die Platte geschrieben oder geloggt und erreicht nie das Sprachmodell. Auf Wunsch merkt
sich Orbwise es eine Zeit lang im Arbeitsspeicher (wie sudo selbst, Standard 15 Minuten) – aber nur für Aufträge
am Rechner, nie für Telegram oder Routinen. Bestätigen muss man jeden Root-Befehl trotzdem.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hmac
import os
import secrets
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

HELPER_TEMPLATE = '''#!{python}
# Orbwise-Askpass: wird von "sudo -A" aufgerufen und fragt das Passwort in der Orbwise-Oberfläche ab.
import json, os, sys, urllib.request
url, token = os.environ.get("ORBWISE_ASKPASS_URL"), os.environ.get("ORBWISE_ASKPASS_TOKEN")
if not url or not token:
    sys.exit(1)
req = urllib.request.Request(url, data=json.dumps({{"prompt": " ".join(sys.argv[1:])}}).encode(), method="POST",
                             headers={{"Content-Type": "application/json", "X-Orbwise-Askpass": token}})
opener = urllib.request.build_opener(urllib.request.ProxyHandler({{}}))  # nie über einen Proxy
try:
    with opener.open(req, timeout=180) as resp:
        password = json.load(resp)["password"]
except Exception:
    sys.exit(1)
sys.stdout.write(password + "\\n")
'''

MAX_USES = 3        # sudo fragt bei falschem Passwort bis zu dreimal
WAIT_SECONDS = 150  # so lange wartet Orbwise auf die Eingabe

Notify = Callable[[dict], Awaitable[None]]

# True während Aufträgen, die nicht am Rechner gegeben wurden (Telegram, Routinen): dann kein gemerktes Passwort
REMOTE: contextvars.ContextVar[bool] = contextvars.ContextVar("orbwise_remote", default=False)


def helper_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/orbwise-{os.getuid()}"
    return Path(runtime) / "orbwise" / "askpass"


def write_helper(path: Path | None = None) -> Path:
    path = path or helper_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(HELPER_TEMPLATE.format(python=sys.executable), encoding="utf-8")
    os.chmod(tmp, 0o700)
    tmp.replace(path)
    return path


class AskpassBroker:
    """Vergibt Einmal-Tokens pro privilegiertem Befehl und vermittelt die Passworteingabe an die Oberfläche."""

    def __init__(self, port: int, notify: Notify | None = None, helper: Path | None = None,
                 has_ui: Callable[[], bool] | None = None, say: Callable[[str], None] | None = None,
                 say_text: str = "Dafür brauche ich dein Passwort. Bitte gib es im Dashboard ein.",
                 remember_seconds: int = 900):
        self.url = f"http://127.0.0.1:{port}/api/askpass"
        self.notify = notify
        self.helper = helper
        self.has_ui = has_ui or (lambda: True)
        self.say = say
        self.say_text = say_text
        self.tokens: dict[str, dict] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self.remember_seconds = max(0, int(remember_seconds))
        self._cached: tuple[str, float] | None = None  # (Passwort, gültig bis) – nur im Arbeitsspeicher
        self._expiry: asyncio.TimerHandle | None = None

    # ---------- gemerktes Passwort ----------
    def cached_until(self) -> float | None:
        if self._cached and self._cached[1] > time.time():
            return self._cached[1]
        if self._cached:
            self._cached = None
        return None

    def _remember(self, password: str) -> None:
        until = time.time() + self.remember_seconds
        self._cached = (password, until)
        self._schedule_expiry()
        self._announce()

    def forget(self) -> None:
        had = self._cached is not None
        self._cached = None
        if self._expiry:
            self._expiry.cancel()
            self._expiry = None
        if had:
            self._announce()

    def _schedule_expiry(self) -> None:
        if self._expiry:
            self._expiry.cancel()
        with contextlib.suppress(RuntimeError):
            self._expiry = asyncio.get_running_loop().call_later(self.remember_seconds + 0.5, self.forget)

    def _announce(self) -> None:
        if self.notify:
            with contextlib.suppress(RuntimeError):
                asyncio.get_running_loop().create_task(
                    self.notify({"type": "sudo_cached", "until": self.cached_until()}))

    def ready(self) -> bool:
        return bool(self.helper and self.helper.exists())

    @contextlib.contextmanager
    def grant(self, command: str, use_cache: bool = True):
        """Umgebung für genau einen privilegierten Befehl; das Token verfällt danach. use_cache=False: das gemerkte
        Passwort gilt nicht – der Dialog erscheint auf jeden Fall (Auto-Modus: er ist dort die einzige Rückfrage)."""
        if not self.ready():
            yield {}
            return
        token = secrets.token_urlsafe(32)
        info = {"uses": 0, "command": command, "local": not REMOTE.get(), "from_cache": False, "candidate": None,
                "no_cache": not use_cache}
        self.tokens[token] = info
        try:
            yield {"SUDO_ASKPASS": str(self.helper), "ORBWISE_ASKPASS_URL": self.url, "ORBWISE_ASKPASS_TOKEN": token}
        finally:
            self.tokens.pop(token, None)
            # Kam nach der letzten Eingabe keine weitere Nachfrage, hat sudo sie akzeptiert → merken
            if info["candidate"] and info["uses"] < MAX_USES and self.remember_seconds:
                self._remember(info["candidate"])

    @contextlib.contextmanager
    def grant_secret(self, secret: str, uses: int = 2):
        """Umgebung für einen Befehl, dem der Askpass-Helfer ein schon bekanntes Passwort reichen soll (ssh mit dem
        im Anmeldedialog eingegebenen Passwort) – ohne erneute Nachfrage, höchstens `uses`-mal."""
        if not self.ready():
            yield {}
            return
        token = secrets.token_urlsafe(32)
        self.tokens[token] = {"secret": secret, "uses": 0, "max": uses}
        try:
            yield {"SSH_ASKPASS": str(self.helper), "SSH_ASKPASS_REQUIRE": "force",
                   "ORBWISE_ASKPASS_URL": self.url, "ORBWISE_ASKPASS_TOKEN": token}
        finally:
            self.tokens.pop(token, None)

    async def ask_login(self, title: str, command: str = "", user: str = "", ask_user: bool = True,
                        remember_text: str = "") -> tuple[str, str, bool] | None:
        """Anmeldedialog in der Oberfläche (Benutzername + Passwort, z. B. für SSH) → (Benutzer, Passwort, merken)
        oder None (abgebrochen, keine Oberfläche, Auftrag vom Handy/aus einer Routine). Die Eingaben gehen nur an
        den Aufrufer – nie ans Sprachmodell, nie auf die Platte."""
        if REMOTE.get() or not self.notify or not self.has_ui():
            return None
        req_id = uuid.uuid4().hex[:10]
        fut = asyncio.get_running_loop().create_future()
        self.pending[req_id] = fut
        await self.notify({"type": "password_request", "id": req_id, "kind": "login", "title": title,
                           "command": command, "prompt": title, "user": user, "ask_user": ask_user,
                           "remember_text": remember_text, "remember": 0})
        if self.say:
            self.say(self.say_text)
        try:
            password, remember, name = await asyncio.wait_for(fut, timeout=WAIT_SECONDS)
        except asyncio.TimeoutError:
            return None
        finally:
            self.pending.pop(req_id, None)
            await self.notify({"type": "password_done", "id": req_id})
        name = (name or user).strip()
        if not password or (ask_user and not name):
            return None
        return name, password, bool(remember)

    def _check(self, token: str) -> dict | None:
        for known, info in self.tokens.items():
            if hmac.compare_digest(known, token or ""):
                return info
        return None

    async def request(self, token: str, prompt: str = "") -> str | None:
        """Vom Helfer aufgerufen. None = abgelehnt/abgebrochen."""
        info = self._check(token)
        if info is not None and "secret" in info:  # schon bekanntes Passwort (grant_secret)
            if info["uses"] >= info["max"]:
                return None
            info["uses"] += 1
            return info["secret"]
        if info is None or info["uses"] >= MAX_USES:
            return None
        cached = self._cached[0] if info["local"] and not info.get("no_cache") and self.cached_until() else None
        if cached is not None and info["uses"] == 0:  # gemerktes Passwort, ohne erneut zu fragen
            info["uses"], info["from_cache"] = 1, True
            return cached
        if info["from_cache"]:  # gemerktes Passwort wurde abgelehnt (z. B. geändert) → vergessen, neu fragen
            self.forget()
            info["from_cache"], info["cache_failed"] = False, True
        if not self.notify or not self.has_ui():
            return None
        info["uses"] += 1
        info["candidate"] = None
        req_id = uuid.uuid4().hex[:10]
        fut = asyncio.get_running_loop().create_future()
        self.pending[req_id] = fut
        retry = info["uses"] > (2 if info.get("cache_failed") else 1)
        await self.notify({"type": "password_request", "id": req_id, "command": info["command"], "retry": retry,
                           "prompt": "Falsches Passwort – bitte erneut eingeben" if retry else "Root-Passwort (sudo)",
                           "remember": int(self.remember_seconds // 60) if info["local"] else 0})
        if self.say and not retry:
            self.say(self.say_text)
        try:
            password, remember, _ = await asyncio.wait_for(fut, timeout=WAIT_SECONDS)
        except asyncio.TimeoutError:
            return None
        finally:
            self.pending.pop(req_id, None)
            await self.notify({"type": "password_done", "id": req_id})
        if password and remember and info["local"] and self.remember_seconds:
            info["candidate"] = password  # gemerkt wird erst, wenn sudo es angenommen hat (siehe grant)
        return password

    def answer(self, req_id: str, password: str | None, remember: bool = True, user: str = "") -> None:
        fut = self.pending.get(req_id)
        if fut and not fut.done():
            fut.set_result((password or None, bool(remember), user or ""))

    def cancel_all(self) -> None:
        for fut in self.pending.values():
            if not fut.done():
                fut.set_result((None, False, ""))


# Vom Server gesetzt; Tools holen sich darüber die Umgebung für sudo -A
BROKER: AskpassBroker | None = None


@contextlib.contextmanager
def privileged_env(command: str, use_cache: bool = True):
    if BROKER is None:
        yield {}
    else:
        with BROKER.grant(command, use_cache) as env:
            yield env
