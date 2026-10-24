"""Freigaben und Datenschutz bei Werkzeug-Aufrufen: was im Auto-Modus ohne Rückfrage laufen darf, wann nach fremden
Inhalten (Mails, Webseiten, Bilder) gefragt wird, und der Wächter gegen Datenabfluss.

Mixin für Agent – nutzt dessen Zustand (self.cfg, self.memory, self._tainted …)."""

from __future__ import annotations

import logging
import os

from . import prompts
from .tools.leakguard import LOCAL_DATA_TOOLS, find_leak, outbound_text
from .tools.registry import (
    BLOCKED,
    CONFIRM,
    SAFE,
    ToolContext,
    coerce_args,
    get_tool,
    missing_args,
)
from .tools.safety import local_read_only

log = logging.getLogger(__name__)

TAINT_SOURCES = {"mail_list", "mail_search", "mail_read", "mail_ask", "daily_briefing", "fetch_url",
                 "look_at_screen", "look_at_image"}  # Webseiten, Bildschirm, Bild: können Anweisungen enthalten

AUTO_TOOLS = ("run_shell", "ssh_run", "write_file", "edit_file")  # Auto-Modus „Auto“ gibt diese ohne Root frei

# Nach dem Lesen fremder Inhalte (Mail, Webseite, Bild) brauchen auch sonst sichere Tools dieser Gruppen eine
# Bestätigung – der Text könnte versteckte Anweisungen enthalten (z. B. Daten per fetch_url nach außen schicken).
TAINT_GUARDED = {"shell", "ssh", "portainer", "web", "files", "apps", "obsidian", "trilium", "calendar_tools",
                 "homeassistant", "memory_tools", "reminder_tools", "power", "telegram_tools"}

# Lesende Werkzeuge dieser Gruppen laufen auch nach fremden Inhalten ohne Rückfrage: lokal lesen kann nichts
# verändern (Geheimes sperrt files.is_secret_path), und was hinausgeht (Suchbegriff, URL) prüft der Datenabfluss-Wächter
TAINT_READ_OK = {"read_file", "list_directory", "find_files", "search_file_contents", "search_nas",
                 "web_search", "fetch_url", "recall", "list_reminders", "calendar_events", "calendar_free",
                 "ha_find", "ha_state", "obsidian_search", "obsidian_read", "obsidian_ask", "trilium_search",
                 "trilium_read", "portainer_containers", "portainer_stacks", "portainer_logs",
                 "portainer_check_updates", "list_installed_apps"}


def running_as_root() -> bool:
    """Läuft Orbwise als root? Dann ist „ohne Root-Rechte“ nicht prüfbar – Auto „Dateien“/„Auto“ fragen wie „Lesen“."""
    return hasattr(os, "geteuid") and os.geteuid() == 0


def is_taint_source(name: str, result: str) -> bool:
    """Bringt dieses Tool-Ergebnis fremden Text (Mails) in den Verlauf?"""
    return name in TAINT_SOURCES and (name != "daily_briefing" or "E-Mail:" in result)


class SafetyMixin:
    @staticmethod
    def _auto_ok(name: str, args: dict, cwd: str | None = None) -> tuple[bool, str]:
        """Auto „Auto“: alles ohne Root – außer Löschen, Ausschalten, Senden ins Netz, Startdateien/Zugangsdaten."""
        from .lang import T
        from .tools.filepolicy import protected_path, writable_path
        from .tools.safety import auto_shell_ok
        if name == "run_shell":
            return auto_shell_ok(str(args.get("command") or ""), cwd)
        if name == "ssh_run":  # auf dem anderen Rechner: dieselben Regeln (sudo fragt dort nach dem Passwort)
            return auto_shell_ok(str(args.get("command") or ""), None)
        path = str(args.get("path") or "")
        if protected_path(path, cwd):
            return False, T("ändert Startdateien, Autostart, Zugangsdaten oder Orbwise selbst – fragt auch im "
                            "Auto-Modus", "changes start-up files, autostart, credentials or Orbwise itself – Auto "
                            "still asks")
        if not writable_path(path, cwd):
            return False, T("nur mit Root-Rechten beschreibbar", "only writable with root privileges")
        return True, ""

    @staticmethod
    def _file_edit_ok(name: str, args: dict, cwd: str | None = None) -> bool:
        """Auto „Dateien“: Dateiänderung im eigenen Home ohne Root/Löschen (siehe tools/filepolicy.py)."""
        from .tools.filepolicy import editable_path
        from .tools.safety import file_edit_ok
        if name in ("write_file", "edit_file"):
            return editable_path(str(args.get("path") or ""), cwd)
        if name == "run_shell":
            return file_edit_ok(str(args.get("command") or ""), cwd)
        return False

    def _final_risk(self, name: str, spec, args: dict, ctx: ToolContext) -> tuple[str, str]:
        """Risiko eines Aufrufs nach Auto-Modus, Planmodus und Schutz vor fremden Inhalten."""
        cwd = ctx.cwd
        risk, reason = spec.assess(ctx, args)
        relaxed = not self._plan and not running_as_root()  # als root gilt nichts als „ohne Root“
        if risk == CONFIRM and self.auto_mode in ("files", "auto") and not self._plan and running_as_root():
            reason = prompts.text(self.cfg, "auto_root") + (f"; {reason}" if reason else "")
        if risk == CONFIRM and self.auto_mode == "files" and relaxed and self._file_edit_ok(name, args, cwd):
            risk, reason = SAFE, prompts.text(self.cfg, "auto_files")
        elif risk == CONFIRM and self.auto_mode == "auto" and relaxed and name in AUTO_TOOLS:
            ok, why = self._auto_ok(name, args, cwd)
            risk, reason = (SAFE, prompts.text(self.cfg, "auto_full")) if ok else (risk, why or reason)
        # Nach fremden Inhalten (Mail, Webseite, Bild, PDF) fragt alles nach, was etwas verändert oder startet –
        # lesende Werkzeuge und rein lesende, lokale Shell-Befehle (echo, cd, ls …) laufen weiter
        if risk == SAFE and getattr(self, "_tainted", False) and spec.group in TAINT_GUARDED \
                and name not in TAINT_READ_OK \
                and not (reason != prompts.text(self.cfg, "auto_full") and name in ("run_shell", "ssh_run")
                         and local_read_only(str(args.get("command") or ""))):
            risk, reason = CONFIRM, prompts.text(self.cfg, "tainted_confirm")
        if risk == SAFE and name in ("run_shell", "ssh_run") and self.auto_mode == "off":
            risk, reason = CONFIRM, prompts.text(self.cfg, "auto_read_off")
        if risk != BLOCKED:  # Datenabfluss-Wächter: private Daten verlassen den Rechner nie unbemerkt
            leak = self._leak(name, args)
            if leak:
                risk, reason = CONFIRM, leak + (f"; {reason}" if risk == CONFIRM and reason else "")
        return risk, reason

    def _leak(self, name: str, args: dict) -> str | None:
        """Würde dieser Aufruf private Daten ins Internet schicken? (siehe tools/leakguard.py)"""
        out = outbound_text(name, args)
        if not out:
            return None
        conv = self.memory.conversation
        msgs = conv.epoch_messages()
        user = next((m.get("content") or "" for m in reversed(msgs) if m.get("role") == "user"), "")
        local = [m.get("full_content") or m.get("content") or "" for m in msgs
                 if m.get("role") == "tool" and m.get("tool_name") in LOCAL_DATA_TOOLS]
        local += [d.get("text") or "" for m in msgs for d in m.get("docs") or ()]  # angehängte PDFs
        return find_leak(out, user if isinstance(user, str) else "", local)

    def _runs_unasked(self, call: dict) -> bool:
        """Läuft dieser Aufruf ohne Rückfrage (und bringt keine fremden Inhalte)? Dann darf er parallel laufen."""
        fn = call.get("function", {})
        name = fn.get("name", "")
        spec = get_tool(name)
        if not spec or not spec.is_enabled(self.cfg) or not self.tool_allowed(name) or name in TAINT_SOURCES:
            return False
        args = coerce_args(spec, fn.get("arguments"))
        if missing_args(spec, args):
            return False
        ctx = ToolContext(cfg=self.cfg, memory=self.memory, cwd=self.project_dir(), output_chars=self.output_chars())
        try:
            return self._final_risk(name, spec, args, ctx)[0] == SAFE
        except Exception:  # noqa: BLE001 – im Zweifel der Reihe nach
            return False
