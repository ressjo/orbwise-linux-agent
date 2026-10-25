"""Zugangsdaten der Skills (Tokens, Passwörter, API-Keys) – nicht im Klartext in config.yaml.

Erste Wahl ist der Schlüsselbund der Sitzung (Secret Service: KWallet, GNOME-Schlüsselbund) über das Paket
`keyring`: verschlüsselt, nach dem Login entsperrt. Gibt es keinen (Server ohne Desktop, kein D-Bus), landen die Werte
in ~/.config/orbwise/secrets.json – nur für den eigenen Benutzer lesbar (0600). `backend()` sagt, was gerade gilt;
Oberfläche und `orbwise doctor` zeigen es an.

$ORBWISE_SECRETS=file erzwingt die Datei (Tests, Server), $ORBWISE_SECRETS_FILE legt ihren Ort fest.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import threading
from pathlib import Path

from .config import config_path, env

log = logging.getLogger(__name__)

SERVICE = "orbwise"
_LOCK = threading.RLock()
_KEYRING: object | None | bool = False  # False = noch nicht geprüft, None = keiner nutzbar


def file_path() -> Path:
    return Path(env("SECRETS_FILE") or config_path().parent / "secrets.json").expanduser()


def _keyring():
    """Nutzbarer Schlüsselbund oder None (fail/null-Backends, kein D-Bus, Paket fehlt)."""
    global _KEYRING
    if _KEYRING is not False:
        return _KEYRING
    _KEYRING = None
    if env("SECRETS").lower() == "file":
        return None
    try:
        import keyring
        from keyring.backends import fail
        kr = keyring.get_keyring()
        if isinstance(kr, fail.Keyring) or "null" in type(kr).__module__ or getattr(kr, "priority", 0) <= 0:
            return None
        kr.get_password(SERVICE, "__probe__")  # Secret Service erreichbar? (entsperrt ggf. per Dialog)
        _KEYRING = kr
    except Exception as e:  # noqa: BLE001 – ohne Schlüsselbund geht es mit der Datei weiter
        log.info("Kein Schlüsselbund nutzbar (%s) – Zugangsdaten in %s", type(e).__name__, file_path())
        _KEYRING = None
    return _KEYRING


def reset() -> None:
    """Backend neu ermitteln (Tests, nach Änderungen an der Umgebung)."""
    global _KEYRING
    _KEYRING = False


def backend() -> str:
    """"keyring" (Schlüsselbund der Sitzung) oder "file" (secrets.json, 0600)."""
    return "keyring" if _keyring() else "file"


def _read_file() -> dict[str, str]:
    try:
        data = json.loads(file_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}


def _write_file(data: dict[str, str]) -> None:
    path = file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if not data:
        path.unlink(missing_ok=True)
        return
    fd, tmp = tempfile.mkstemp(prefix=".secrets.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def get(key: str) -> str:
    """Gespeicherter Wert oder "" – erst der Schlüsselbund, dann die Datei."""
    kr = _keyring()
    if kr is not None:
        try:
            value = kr.get_password(SERVICE, key)
            if value:
                return value
        except Exception as e:  # noqa: BLE001
            log.warning("Schlüsselbund nicht lesbar (%s)", type(e).__name__)
    with _LOCK:
        return _read_file().get(key, "")


def set(key: str, value: str) -> str:  # noqa: A001 – wie keyring.set_password
    """Speichern (leer = löschen). → Backend, in dem der Wert jetzt liegt."""
    if not value:
        delete(key)
        return backend()
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(SERVICE, key, value)
            with _LOCK:  # eine ältere Kopie in der Datei weg
                data = _read_file()
                if data.pop(key, None) is not None:
                    _write_file(data)
            return "keyring"
        except Exception as e:  # noqa: BLE001 – dann eben in die Datei
            log.warning("Schlüsselbund nicht beschreibbar (%s) – speichere in %s", type(e).__name__, file_path())
    with _LOCK:
        data = _read_file()
        data[key] = value
        _write_file(data)
    return "file"


def delete(key: str) -> None:
    kr = _keyring()
    if kr is not None:
        with contextlib.suppress(Exception):
            kr.delete_password(SERVICE, key)
    with _LOCK:
        data = _read_file()
        if data.pop(key, None) is not None:
            _write_file(data)


def has(key: str) -> bool:
    return bool(get(key))
