"""Zugangsdaten: Schlüsselbund der Sitzung, ohne ihn eine nur für den Benutzer lesbare Datei."""

import stat

import keyring
from keyring.backend import KeyringBackend

from orbwise import keystore


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self):
        super().__init__()
        self.data = {}

    def get_password(self, service, username):
        return self.data.get((service, username))

    def set_password(self, service, username, password):
        self.data[(service, username)] = password

    def delete_password(self, service, username):
        self.data.pop((service, username), None)


def test_file_fallback_is_private(tmp_path):
    assert keystore.backend() == "file"
    assert keystore.set("paperless.token", "geheim") == "file"
    path = keystore.file_path()
    assert path == tmp_path / "secrets.json" and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert keystore.get("paperless.token") == "geheim" and keystore.has("paperless.token")
    keystore.set("paperless.token", "")  # leer = löschen
    assert keystore.get("paperless.token") == "" and not path.exists()


def test_keyring_is_preferred_and_moves_file_copies(monkeypatch):
    keystore.set("mail.password", "alt")  # lag noch in der Datei
    kr = MemoryKeyring()
    monkeypatch.delenv("ORBWISE_SECRETS")
    monkeypatch.setattr(keyring, "get_keyring", lambda: kr)
    keystore.reset()
    assert keystore.backend() == "keyring"
    assert keystore.get("mail.password") == "alt"  # aus der Datei, solange der Schlüsselbund es nicht hat
    assert keystore.set("mail.password", "neu") == "keyring"
    assert kr.data[("orbwise", "mail.password")] == "neu" and not keystore.file_path().exists()
    keystore.delete("mail.password")
    assert keystore.get("mail.password") == ""


def test_broken_keyring_falls_back_to_the_file(monkeypatch):
    class Broken(MemoryKeyring):
        def get_password(self, service, username):
            raise RuntimeError("kein D-Bus")
    monkeypatch.delenv("ORBWISE_SECRETS")
    monkeypatch.setattr(keyring, "get_keyring", lambda: Broken())
    keystore.reset()
    assert keystore.backend() == "file" and keystore.set("x", "1") == "file" and keystore.get("x") == "1"
