"""Freigaben und Datenschutz bei Werkzeug-Aufrufen: was im Auto-Modus ohne Rückfrage laufen darf, wann nach fremden
Inhalten (Mails, Webseiten, Bilder) gefragt wird, und der Wächter gegen Datenabfluss.

Mixin für Agent – nutzt dessen Zustand (self.cfg, self.memory, self._tainted …)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

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

TAINT_SOURCES = {"mail_list", "mail_search", "mail_read", "mail_ask", "daily_briefing", "fetch_url", "webfetch",
                 "look_at_screen", "look_at_image", "screenshot"}  # Webseiten, Bildschirm, Bild: können Anweisungen enthalten

# Auto-Modus „Auto“ gibt diese ohne Root frei (shell/write/edit: die Coding-Werkzeuge nach OpenCode)
AUTO_TOOLS = ("run_shell", "ssh_run", "write_file", "edit_file", "shell", "write", "edit")
SHELL_TOOLS = ("run_shell", "ssh_run", "shell")
LOCAL_SHELLS = ("run_shell", "shell")  # laufen auf diesem Rechner – im Auto-Modus im Käfig (jail.py)
# Sandbox (außer Auto „Aus“): diese Gruppen laufen ohne Rückfrage – Dateien nur in der Arbeitskopie, Lesen außerhalb
# der Kopie (SANDBOX_READS) fragt
SANDBOX_GROUPS = {"code", "todo_tools", "memory_tools"}
SANDBOX_READS = ("read", "grep", "glob")
ALWAYS_ASK = {"github_push", "github_clone"}  # fragen immer – auch im Auto-Modus „Auto“ und in der Sandbox
FILE_TOOLS = ("write_file", "edit_file", "write", "edit")

# Nach dem Lesen fremder Inhalte (Mail, Webseite, Bild) brauchen auch sonst sichere Tools dieser Gruppen eine
# Bestätigung – der Text könnte versteckte Anweisungen enthalten (z. B. Daten per fetch_url nach außen schicken).
TAINT_GUARDED = {"shell", "ssh", "portainer", "web", "files", "code", "apps", "obsidian", "trilium", "calendar_tools",
                 "homeassistant", "memory_tools", "reminder_tools", "power", "telegram_tools"}

# Lesende Werkzeuge dieser Gruppen laufen auch nach fremden Inhalten ohne Rückfrage: lokal lesen kann nichts
# verändern (Geheimes sperrt files.is_secret_path), und was hinausgeht (Suchbegriff, URL) prüft der Datenabfluss-Wächter
TAINT_READ_OK = {"read_file", "list_directory", "find_files", "search_file_contents", "search_nas",
                 "web_search", "fetch_url", "recall", "list_reminders", "calendar_events", "calendar_free",
                 "ha_find", "ha_state", "obsidian_search", "obsidian_read", "obsidian_ask", "trilium_search",
                 "trilium_read", "portainer_containers", "portainer_stacks", "portainer_logs",
                 "portainer_check_updates", "list_installed_apps",
                 "read", "grep", "glob", "websearch", "webfetch", "job_output", "question", "screenshot"}


def running_as_root() -> bool:
    """Läuft Orbwise als root? Dann ist „ohne Root-Rechte“ nicht prüfbar – Auto „Dateien“/„Auto“ fragen wie „Lesen“."""
    return hasattr(os, "geteuid") and os.geteuid() == 0


def is_taint_source(name: str, result: str) -> bool:
    """Bringt dieses Tool-Ergebnis fremden Text (Mails) in den Verlauf?"""
    if name == "screenshot":  # eigene Seite (localhost, Projektdatei) ist kein fremder Inhalt – eine Webseite schon
        return "Screenshot einer Webseite" in result
    return name in TAINT_SOURCES and (name != "daily_briefing" or "E-Mail:" in result)


class SafetyMixin:
    @staticmethod
    def _auto_ok(name: str, args: dict, cwd: str | None = None, project: str | None = None) -> tuple[bool, str]:
        """Auto „Auto“: alles ohne Root – außer Löschen, Ausschalten, Senden ins Netz, Startdateien/Zugangsdaten.
        Coding-Modus mit Projektordner: Dateien außerhalb des Projekts fragen."""
        from .lang import T
        from .tools.filepolicy import in_project, protected_path, writable_path
        from .tools.safety import auto_shell_ok
        if name in ("run_shell", "shell"):
            return auto_shell_ok(str(args.get("command") or ""), SafetyMixin._shell_cwd(name, args, cwd))
        if name == "ssh_run":  # auf dem anderen Rechner: dieselben Regeln (sudo fragt dort nach dem Passwort)
            return auto_shell_ok(str(args.get("command") or ""), None)
        path = str(args.get("path") or "")
        if project and not in_project(path, project, cwd, literal=True):
            return False, T("liegt außerhalb des Projektordners", "outside the project folder")
        if protected_path(path, cwd, literal=True):
            return False, T("ändert Startdateien, Autostart, Zugangsdaten oder Orbwise selbst – fragt auch im "
                            "Auto-Modus", "changes start-up files, autostart, credentials or Orbwise itself – Auto "
                            "still asks")
        if not writable_path(path, cwd):
            return False, T("nur mit Root-Rechten beschreibbar", "only writable with root privileges")
        return True, ""

    @staticmethod
    def _file_edit_ok(name: str, args: dict, cwd: str | None = None, project: str | None = None) -> bool:
        """Auto „Dateien“: Dateiänderung im eigenen Home ohne Root/Löschen (siehe tools/filepolicy.py) – im
        Coding-Modus mit Projektordner genau die Dateien im Projekt."""
        from .tools.filepolicy import editable_path
        from .tools.safety import file_edit_ok
        if name in FILE_TOOLS:
            return editable_path(str(args.get("path") or ""), cwd, project, literal=True)
        if name in ("run_shell", "shell"):
            return file_edit_ok(str(args.get("command") or ""), SafetyMixin._shell_cwd(name, args, cwd))
        return False

    @staticmethod
    def _shell_cwd(name: str, args: dict, cwd: str | None) -> str | None:
        """Arbeitsordner eines Shell-Aufrufs (shell kennt workdir)."""
        workdir = str(args.get("workdir") or "") if name == "shell" else ""
        if not workdir:
            return cwd
        p = os.path.expanduser(workdir)
        return p if os.path.isabs(p) else os.path.join(cwd or os.path.expanduser("~"), p)

    def _final_risk(self, name: str, spec, args: dict, ctx: ToolContext) -> tuple[str, str]:
        """Risiko eines Aufrufs nach Auto-Modus, Planmodus und Schutz vor fremden Inhalten."""
        cwd = ctx.cwd
        project = self.project_dir() if hasattr(self, "project_dir") else None
        risk, reason = spec.assess(ctx, args)
        sb = self.sandbox_state() if hasattr(self, "sandbox_state") else None
        if sb and self.auto_mode != "off" and not self._plan and spec.group in SANDBOX_GROUPS:
            # Container + Arbeitskopie: Bauen, Testen, Installieren (auch sudo – root im Container ist nicht root auf
            # dem Rechner) läuft ohne Rückfrage, auch nach fremden Inhalten. Damit dann nichts Privates hinausgeht,
            # fragt Lesen außerhalb der Arbeitskopie; der Datenabfluss-Wächter bleibt.
            if risk == CONFIRM and (name == "shell" or (name in ("write", "edit")
                                                        and self._in_sandbox(name, args, cwd, sb))):
                risk, reason = SAFE, prompts.text(self.cfg, "auto_sandbox")
            elif risk == CONFIRM and name in ("write", "edit"):
                reason = prompts.text(self.cfg, "sandbox_outside_write") + (f" ({reason})" if reason else "")
            elif risk == SAFE and name in SANDBOX_READS and not self._reads_in_copy(args, cwd, sb):
                risk, reason = CONFIRM, prompts.text(self.cfg, "sandbox_outside_read")
            elif risk == SAFE and name == "shell":
                reason = prompts.text(self.cfg, "auto_sandbox")
            leak = self._leak(name, args) if risk != BLOCKED else None
            if leak:
                risk, reason = CONFIRM, leak
            return risk, reason
        relaxed = not self._plan and not running_as_root()  # als root gilt nichts als „ohne Root“
        if risk == CONFIRM and self.auto_mode in ("files", "auto") and not self._plan and running_as_root():
            reason = prompts.text(self.cfg, "auto_root") + (f"; {reason}" if reason else "")
        if risk == CONFIRM and self.auto_mode == "files" and relaxed and self._file_edit_ok(name, args, cwd, project):
            risk, reason = SAFE, prompts.text(self.cfg, "auto_project" if project and name in FILE_TOOLS
                                              else "auto_files")
        elif risk == CONFIRM and self.auto_mode == "auto" and relaxed and name in AUTO_TOOLS:
            ok, why = self._auto_ok(name, args, cwd, project)
            if ok and name in LOCAL_SHELLS:
                risk, reason = self._jailed(ctx, project)
            else:
                risk, reason = (SAFE, prompts.text(self.cfg, "auto_full")) if ok else (risk, why or reason)
        # Nach fremden Inhalten (Mail, Webseite, Bild, PDF) fragt alles nach, was etwas verändert oder startet –
        # lesende Werkzeuge und rein lesende, lokale Shell-Befehle (echo, cd, ls …) laufen weiter
        if risk == SAFE and getattr(self, "_tainted", False) and spec.group in TAINT_GUARDED \
                and name not in TAINT_READ_OK \
                and not (not getattr(ctx, "jail", None) and reason != prompts.text(self.cfg, "auto_full") and name in SHELL_TOOLS
                         and local_read_only(str(args.get("command") or ""))):
            risk, reason = CONFIRM, prompts.text(self.cfg, "tainted_confirm")
        if risk == SAFE and name in SHELL_TOOLS and self.auto_mode == "off":
            risk, reason = CONFIRM, prompts.text(self.cfg, "auto_read_off")
        if risk != BLOCKED:  # Datenabfluss-Wächter: private Daten verlassen den Rechner nie unbemerkt
            leak = self._leak(name, args)
            if leak:
                risk, reason = CONFIRM, leak + (f"; {reason}" if risk == CONFIRM and reason else "")
        if name in ALWAYS_ASK and risk == SAFE:
            risk = CONFIRM
        if risk != SAFE:
            ctx.jail = None  # was du bestätigst, läuft wie gesehen – ohne Käfig
        return risk, reason

    def _jailed(self, ctx: ToolContext, project: str | None) -> tuple[str, str]:
        """Auto: Shell-Befehl ohne Rückfrage – aber nur im Käfig (bubblewrap). Ohne Käfig fragt er nach."""
        from . import jail
        ok, why = jail.available()
        if not ok:
            return CONFIRM, prompts.text(self.cfg, "auto_no_jail").format(reason=why)
        writable = ([project] if project else []) + list(getattr(self.cfg.tools, "jail_writable", []) or [])
        ctx.jail = {"writable": writable, "key": getattr(self.memory.conversation, "chat_id", "") or "tools",
                    "where": prompts.text(self.cfg, "auto_jail_where_project" if project else "auto_jail_where_tmp"),
                    "allowed": ", ".join([*writable, "/tmp"])}
        return SAFE, prompts.text(self.cfg, "auto_jail").format(where=ctx.jail["where"])

    @staticmethod
    def _reads_in_copy(args: dict, cwd: str | None, sb: dict) -> bool:
        """Liest read/grep/glob nur in der Arbeitskopie? Ohne Pfad: der Projektordner (= die Kopie)."""
        from .tools.filepolicy import in_project
        path = str(args.get("path") or "")
        return not path or bool(in_project(path, sb["path"], cwd, literal=True))

    @staticmethod
    def _in_sandbox(name: str, args: dict, cwd: str | None, sb: dict) -> bool:
        """Betrifft der Aufruf nur die Arbeitskopie? (Shell läuft ohnehin im Container – nur der Arbeitsordner zählt.)"""
        from .tools.filepolicy import in_project
        if name == "shell":
            workdir = str(args.get("workdir") or "")
            return not workdir or bool(in_project(workdir, sb["path"], cwd))
        path = str(args.get("path") or "")
        rel_ok = bool(in_project(path, sb["path"], cwd, literal=True))
        return rel_ok and ".git" not in Path(path).parts

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
        ctx = ToolContext(cfg=self.cfg, memory=self.memory, cwd=self.project_dir(), output_chars=self.output_chars(),
                          sandbox=self.sandbox_state() if hasattr(self, "sandbox_state") else None)
        try:
            return self._final_risk(name, spec, args, ctx)[0] == SAFE
        except Exception:  # noqa: BLE001 – im Zweifel der Reihe nach
            return False
