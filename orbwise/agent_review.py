"""Code-Check (🛡 im Coding-Modus): das Projekt in der Sandbox prüfen – so gut, wie es mit demselben Modell geht.

1. Feste Prüfungen zuerst (review_checks.py): Tests, Linter, Typprüfung, Sicherheits-Scanner. Ihre Funde sind echt.
2. Reviewer mit frischem Blick: je ein Unteragent mit leerem Kontext (sieht Code und Änderungen, nicht die Begründungen
   aus dem Chat) – ein Thema je Durchgang: Fehler, Sicherheit, Struktur.
3. Ein skeptischer Prüfer muss jeden Fund am Code belegen (lesen, ggf. mit einem Befehl beweisen) – unbelegte fliegen
   raus. Das filtert die erfundenen Funde, die größte Schwäche eines Selbst-Reviews.
4. Bericht im Chat; bestätigte Funde und fehlschlagende Prüfungen behebt der Agent danach selbst (Hub.review), die
   Prüfungen laufen erneut – höchstens coding.review_rounds Runden.

Mixin für Agent – nutzt dessen Unteragenten (run_subagent), Sandbox und Sperre."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid

from . import prompts, review_checks
from .lang import T
from .memory import est_tokens
from .tools.registry import ToolContext

log = logging.getLogger(__name__)

FOCUSES = ("bugs", "security", "structure")
FOCUS_LABEL = {"bugs": ("Fehler und Logik", "Bugs and logic"), "security": ("Sicherheit", "Security"),
               "structure": ("Struktur", "Structure")}
SEVERITY = {"hoch": "high", "high": "high", "kritisch": "high", "critical": "high", "mittel": "medium",
            "medium": "medium", "niedrig": "low", "low": "low", "gering": "low"}
SEV_ICON = {"high": "🔴", "medium": "🟠", "low": "🟡"}
MAX_CANDIDATES = 15
FULL_FILES = 80  # „ganzes Projekt“: so viele Quelldateien höchstens in der Liste
SOURCE = re.compile(r"\.(py|js|jsx|ts|tsx|mjs|cjs|go|rs|java|kt|rb|php|c|h|cc|cpp|hpp|cs|swift|sh|bash|sql|vue|svelte|"
                    r"html|css|scss|ya?ml|toml|json)$", re.I)
class ReviewStopped(Exception):
    """Der Nutzer hat den Code-Check in der Aktivität gestoppt."""


SKIP_DIRS = re.compile(r"(^|/)(node_modules|vendor|dist|build|\.venv|venv|__pycache__|\.git|target|coverage)/")


def json_list(text: str) -> list:
    """Letzte JSON-Liste in einer Modellantwort (auch in ```json … ``` oder mit Text davor)."""
    text = re.sub(r"```(?:json)?", "", text or "")
    for start in [m.start() for m in re.finditer(r"\[", text)][::-1]:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "[":
                depth += 1
            elif text[i] == "]":
                depth -= 1
                if depth == 0:
                    try:
                        value = json.loads(text[start:i + 1])
                    except ValueError:
                        break
                    if isinstance(value, list) and all(isinstance(v, dict) for v in value):
                        return value
                    break
    return []


def clean_findings(raw: list, focus: str) -> list[dict]:
    out = []
    for f in raw:
        if not isinstance(f, dict) or not (f.get("title") or f.get("why")):
            continue
        try:
            line = int(f.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        out.append({"focus": focus, "severity": SEVERITY.get(str(f.get("severity", "")).lower(), "low"),
                    "file": str(f.get("file") or "").strip(), "line": line,
                    "title": str(f.get("title") or "").strip()[:160], "why": str(f.get("why") or "").strip()[:800],
                    "fix": str(f.get("fix") or "").strip()[:600]})
    return out


def confirmed(findings: list[dict], verdicts: list) -> list[dict]:
    """Nur die Funde, die der Prüfer bestätigt hat (Antwort fehlt → keiner)."""
    ok = {int(v.get("id", 0)) for v in verdicts if isinstance(v, dict)
          and str(v.get("verdict", "")).lower().startswith(("bestätigt", "confirmed", "best", "conf"))}
    reasons = {int(v.get("id", 0)): str(v.get("reason") or "") for v in verdicts if isinstance(v, dict)}
    return [{**f, "proof": reasons.get(i, "")} for i, f in enumerate(findings, 1) if i in ok]


class ReviewMixin:
    # ---------------------------------------------------------------- Ablauf
    async def code_review(self, emit, confirm, full: bool = False, think=None) -> dict:
        """Prüfen und berichten. → {"fix": Auftrag zum Beheben (leer = nichts zu tun), "files": [...]}"""
        async with self.lock:
            self._plan, self._approved_plan = False, ""
            self._set_think(think)
            await self._fit_think()
            self._review_stop, self._review_running = False, True
            try:
                return await self._review(emit, confirm, full)
            finally:
                self._think, self._think_level = None, None
                self._review_running = False

    async def code_recheck(self, emit, confirm, previous: dict, round_: int, last: bool = False) -> dict:
        """Nach dem Beheben: nur die festen Prüfungen erneut. → wie code_review (fix leer = alles grün)."""
        async with self.lock:
            self._review_running = True
            try:
                return await self._recheck(emit, confirm, previous, round_, last)
            finally:
                self._review_running = False

    def stop_review(self) -> bool:
        """„Stoppen“ in der Aktivität: laufenden Schritt abbrechen, restliche Durchgänge auslassen, Bericht mit dem
        Stand bis dahin – ohne automatisches Beheben. False, wenn gerade kein Code-Check läuft."""
        if not self._review_running:
            return False
        self._review_stop = True
        task = self._review_task
        if task is not None and not task.done():
            task.cancel()
        return True

    def review_stopped(self) -> bool:
        return self._review_stop

    async def _stoppable(self, coro):
        """Einen Schritt so laufen lassen, dass stop_review ihn abbrechen kann (→ ReviewStopped). Ein Abbruch von
        außen (STOP für alles) geht unverändert weiter."""
        task = asyncio.ensure_future(coro)
        self._review_task = task
        try:
            return await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if self.review_stopped() and not (current and current.cancelling()):
                raise ReviewStopped from None
            raise
        finally:
            self._review_task = None

    async def _review(self, emit, confirm, full: bool) -> dict:
        en = self.cfg.language == "en"
        conv = self.memory.conversation
        conv.add({"role": "user", "content": T("🛡 Code-Check", "🛡 Code check") + (
            T(" (ganzes Projekt)", " (whole project)") if full else ""), "review": True})
        msg_id = uuid.uuid4().hex[:8]
        await emit({"type": "assistant_start", "id": msg_id, "plan": False})
        if self.mode != "coding" or not self.real_project():
            return await self._review_post(emit, msg_id, T("Der Code-Check braucht einen Coding-Chat mit Projektordner.",
                                                           "The code check needs a coding chat with a project folder."))
        from . import sandbox
        sb = self.sandbox_state()
        if not sb:
            sid = await self._review_step(emit, T("Sandbox einschalten", "Turn on the sandbox"))
            try:
                sb = await self.sandbox_enable(lambda t: emit({"type": "tool_output", "id": sid, "text": t + "\n"}))
            except Exception as e:  # noqa: BLE001 – keine Container-Engine usw.
                await self._review_done(emit, sid, str(e), False)
                return await self._review_post(emit, msg_id, T(
                    f"Der Code-Check läuft in der Sandbox – sie ließ sich nicht starten: {e}",
                    f"The code check runs in the sandbox – it could not be started: {e}"))
            await self._review_done(emit, sid, sb.get("path", ""), True)
            await emit({"type": "sandbox_state", "sandbox": {
                "enabled": True, "network": sb.get("network", True), "branch": sb.get("branch", ""),
                "engine": sb.get("engine", ""), "path": sb.get("path", "")}})
        try:
            binary = await sandbox.ensure_running(self.cfg, sb)
        except Exception as e:  # noqa: BLE001
            return await self._review_post(emit, msg_id, T(f"Sandbox nicht verfügbar: {e}", f"Sandbox unavailable: {e}"))

        sid = await self._review_step(emit, T("Was wird geprüft?", "What is checked?"))
        files, diff, scope = await self._review_scope(sb, binary, full)
        await self._review_done(emit, sid, f"{len(files)} " + T("Dateien", "files") + f" · {scope}", True)
        if not files:
            return await self._review_post(emit, msg_id, T(
                "Nichts zu prüfen: keine Änderungen seit dem letzten Commit. Für alles: /review all.",
                "Nothing to check: no changes since the last commit. For everything: /review all."))

        checks = await self._review_checks(emit, sb, binary, files)
        findings: list[dict] = []
        for focus in FOCUSES:
            if self.review_stopped():
                break
            findings += await self._review_pass(emit, confirm, sb, focus, files, diff, checks)
        order = {"high": 0, "medium": 1, "low": 2}
        findings.sort(key=lambda f: order[f["severity"]])
        candidates = findings[:MAX_CANDIDATES]
        verified = await self._review_verify(emit, confirm, sb, candidates) \
            if candidates and not self.review_stopped() else []
        if self.review_stopped():  # Stand bis dahin, nichts gegengeprüft und nichts automatisch beheben
            report = self._review_report(scope, files, checks, [], len(findings), en, unverified=findings)
            await self._review_post(emit, msg_id, report)
            return {"fix": "", "files": files, "checks": checks}
        report = self._review_report(scope, files, checks, verified, len(findings), en)
        await self._review_post(emit, msg_id, report)
        failing = [c for c in checks if c["status"] == "fail"]
        todo = [f for f in verified if f["severity"] in ("high", "medium")]
        fix = self._fix_prompt(checks, failing, todo) if failing or todo else ""
        return {"fix": fix, "files": files, "checks": checks}

    async def _recheck(self, emit, confirm, previous: dict, round_: int, last: bool = False) -> dict:
        from . import sandbox
        sb = self.sandbox_state()
        if not sb:
            return {"fix": ""}
        msg_id = uuid.uuid4().hex[:8]
        await emit({"type": "assistant_start", "id": msg_id, "plan": False})
        binary = await sandbox.ensure_running(self.cfg, sb)
        checks = await self._review_checks(emit, sb, binary, previous.get("files") or [])
        if self.review_stopped():
            await self._review_post(emit, msg_id, T("⏹ Code-Check gestoppt – mit /review geht es von vorn los.",
                                                    "⏹ Code check stopped – /review starts over."))
            return {"fix": "", "files": previous.get("files"), "checks": checks}
        failing = [c for c in checks if c["status"] == "fail"]
        lines = [self._check_line(c) for c in checks]
        head = T(f"Prüfungen nach dem Beheben (Runde {round_}):", f"Checks after fixing (round {round_}):")
        verdict = (T("Alle Prüfungen sind grün. ✔", "All checks are green. ✔") if not failing
                   else T(f"Nach {round_} Runden noch nicht grün – die offenen Prüfungen stehen oben; mit /review "
                          "geht es weiter.", f"Not green after {round_} rounds – the open checks are listed above; "
                          "/review continues.") if last else T("Noch nicht grün – weiter.", "Not green yet – continuing."))
        await self._review_post(emit, msg_id, "\n".join([f"**{head}**", *lines, "", verdict]))
        if not failing:
            return {"fix": "", "files": previous.get("files"), "checks": checks}
        failing_text = "\n\n".join(f"### {review_checks.label(c['name'], self.cfg.language == 'en')}\n`{c['cmd']}`\n"
                                   f"```\n{c['output'][-3000:]}\n```" for c in failing)
        fix = prompts.review_text(self.cfg, "fix_again").format(round=round_ + 1, failing=failing_text)
        return {"fix": fix, "files": previous.get("files"), "checks": checks}

    # ---------------------------------------------------------------- Bausteine
    async def _review_step(self, emit, label: str) -> str:
        sid = "rv" + uuid.uuid4().hex[:6]
        await emit({"type": "tool_call", "id": sid, "name": "code_review", "args": {"step": label}, "risk": "safe",
                    "reason": "", "group": "code"})
        return sid

    @staticmethod
    async def _review_done(emit, sid: str, text: str, ok: bool) -> None:
        await emit({"type": "tool_result", "id": sid, "status": "ok" if ok else "error", "text": text})

    @staticmethod
    async def _review_halted(emit, sid: str) -> None:
        await emit({"type": "tool_result", "id": sid, "status": "denied", "text": T("gestoppt", "stopped")})

    async def _review_post(self, emit, msg_id: str, text: str) -> dict:
        await emit({"type": "token", "id": msg_id, "text": text})
        self.memory.conversation.add({"role": "assistant", "content": text, "review": True})
        self.memory.conversation.save()
        await emit({"type": "assistant_end", "id": msg_id, "text": text})
        return {"fix": ""}

    async def _sb_run(self, emit, sid: str, sb: dict, binary: str, script: str, timeout: float) -> tuple[int | None, str]:
        from . import sandbox
        from .tools import proc
        ctx = ToolContext(cfg=self.cfg, memory=self.memory, emit=emit, call_id=sid)
        return await proc.run(ctx, sandbox.exec_argv(binary, sb["name"], sb["path"], script, timeout),
                              timeout=timeout + 30, cwd=sb["path"])

    async def _review_scope(self, sb: dict, binary: str, full: bool) -> tuple[list[str], str, str]:
        """(Dateien, Diff-Ausschnitt, Beschreibung). Standard: Änderungen in der Sandbox, sonst seit dem letzten Commit,
        sonst der letzte Commit; full: alle Quelldateien (gekürzt)."""
        from . import workspace
        limit = int(self.context_budget() * 0.25 * 3)  # Zeichen für den Diff je Durchgang
        if full:
            rc, out = await self._sb_run(None, "", sb, binary, "git ls-files 2>/dev/null || find . -type f", 60)
            files = [f.removeprefix("./") for f in out.splitlines() if SOURCE.search(f) and not SKIP_DIRS.search(f)]
            return files[:FULL_FILES], "", T("ganzes Projekt", "whole project") + (
                T(f" (die ersten {FULL_FILES})", f" (first {FULL_FILES})") if len(files) > FULL_FILES else "")
        changed = [c for c in await asyncio.to_thread(workspace.changes, sb) if c["status"] != "deleted"]
        if changed:
            parts, used = [], 0
            for c in changed:
                d = await asyncio.to_thread(workspace.diff, sb, c["path"])
                if used + len(d) > limit:
                    parts.append(T(f"(Diff von {c['path']} gekürzt – lies die Datei)",
                                   f"(diff of {c['path']} cut – read the file)"))
                    continue
                parts.append(d)
                used += len(d)
            return [c["path"] for c in changed], "\n".join(parts), T("Änderungen in der Sandbox",
                                                                      "changes in the sandbox")
        # Keine Änderung in der Sandbox: was seit dem letzten Commit offen ist, sonst der letzte Commit
        for label, cmd in ((T("seit dem letzten Commit", "since the last commit"),
                            "{ git diff --name-only HEAD; git ls-files --others --exclude-standard; } | sort -u"),
                           (T("letzter Commit", "last commit"), "git diff --name-only HEAD~1 HEAD")):
            rc, out = await self._sb_run(None, "", sb, binary, cmd + " 2>/dev/null", 60)
            files = [f for f in out.splitlines() if f.strip() and not SKIP_DIRS.search(f)]
            if files:
                diff_cmd = "git diff HEAD" if "HEAD~1" not in cmd else "git diff HEAD~1 HEAD"
                rc, diff = await self._sb_run(None, "", sb, binary, diff_cmd + " 2>/dev/null", 60)
                return files, diff[:limit], label
        return [], "", ""

    async def _review_checks(self, emit, sb: dict, binary: str, files: list[str]) -> list[dict]:
        sid = await self._review_step(emit, T("Tests, Linter und Scanner", "Tests, linters and scanners"))
        try:
            rc, out = await self._stoppable(self._sb_run(emit, sid, sb, binary, review_checks.build_script(files),
                                                         review_checks.TIMEOUT * 4))
        except ReviewStopped:
            await self._review_halted(emit, sid)
            return []
        checks = review_checks.parse(out)
        bad = [c for c in checks if c["status"] == "fail"]
        await self._review_done(emit, sid, " · ".join(self._check_line(c, short=True) for c in checks)
                                or T("keine passenden Prüfungen", "no matching checks"), not bad)
        return checks

    def _check_line(self, c: dict, short: bool = False) -> str:
        en = self.cfg.language == "en"
        icon = {"ok": "✔", "fail": "✘", "skip": "–"}[c["status"]]
        name = review_checks.label(c["name"], en)
        if short:
            return f"{icon} {name}"
        note = "" if c["status"] != "skip" else f" ({c['output']})"
        return f"- {icon} {name}{note}"

    async def _review_pass(self, emit, confirm, sb: dict, focus: str, files: list[str], diff: str,
                           checks: list[dict]) -> list[dict]:
        en = self.cfg.language == "en"
        label = FOCUS_LABEL[focus][1 if en else 0]
        sid = await self._review_step(emit, T("Durchgang: ", "Pass: ") + label)
        relevant = [c for c in checks if c["status"] == "fail"
                    and ((c["name"] in review_checks.SECURITY) == (focus == "security") or focus == "bugs")]
        checks_text = "".join(f"{T('Ergebnis', 'Result')} {review_checks.label(c['name'], en)} (`{c['cmd']}`):\n"
                              f"```\n{c['output'][-2500:]}\n```\n\n" for c in relevant)
        if focus == "structure":
            checks_text = ""
        diff_text = f"{T('Änderungen (Diff)', 'Changes (diff)')}:\n```diff\n{diff}\n```\n\n" if diff else ""
        task = prompts.review_text(self.cfg, "pass").format(
            focus=prompts.review_text(self.cfg, focus), project=sb["path"],
            files="\n".join(f"- {f}" for f in files[:FULL_FILES]), diff=diff_text,
            checks=(T("Prüfwerkzeuge melden:\n", "Check tools report:\n") + checks_text) if checks_text else "")
        ctx = ToolContext(cfg=self.cfg, memory=self.memory, emit=emit, call_id=sid, cwd=sb["path"], confirm=confirm,
                          sandbox=sb, agent=self, output_chars=self.output_chars())
        try:
            answer = await self._stoppable(self.run_subagent(ctx, "explore", label, task))
        except ReviewStopped:
            await self._review_halted(emit, sid)
            return []
        except Exception as e:  # noqa: BLE001 – ein Durchgang darf scheitern, die anderen laufen weiter
            log.warning("Review-Durchgang %s fehlgeschlagen: %s", focus, e)
            await self._review_done(emit, sid, str(e), False)
            return []
        found = clean_findings(json_list(answer), focus)
        await self._review_done(emit, sid, T(f"{len(found)} mögliche Funde", f"{len(found)} possible findings"), True)
        return found

    async def _review_verify(self, emit, confirm, sb: dict, candidates: list[dict]) -> list[dict]:
        sid = await self._review_step(emit, T("Funde gegenprüfen", "Verify findings"))
        listing = "\n".join(f"{i}. [{f['severity']}] {f['file']}:{f['line']} – {f['title']}: {f['why']}"
                            for i, f in enumerate(candidates, 1))
        task = prompts.review_text(self.cfg, "verify").format(findings=listing)
        ctx = ToolContext(cfg=self.cfg, memory=self.memory, emit=emit, call_id=sid, cwd=sb["path"], confirm=confirm,
                          sandbox=sb, agent=self, output_chars=self.output_chars())
        try:
            answer = await self._stoppable(self.run_subagent(ctx, "explore", T("Gegenprüfung", "Verification"), task))
        except ReviewStopped:
            await self._review_halted(emit, sid)
            return []
        except Exception as e:  # noqa: BLE001
            log.warning("Gegenprüfung fehlgeschlagen: %s", e)
            await self._review_done(emit, sid, str(e), False)
            return []
        verified = confirmed(candidates, json_list(answer))
        await self._review_done(emit, sid, T(f"{len(verified)} von {len(candidates)} bestätigt",
                                             f"{len(verified)} of {len(candidates)} confirmed"), True)
        return verified

    def _review_report(self, scope: str, files: list[str], checks: list[dict], verified: list[dict], total: int,
                       en: bool, unverified: list[dict] | None = None) -> str:
        """unverified (nicht None): Check wurde gestoppt – diese Funde sind nicht gegengeprüft, nichts wird behoben."""
        out = [f"## {T('Code-Check', 'Code check')}",
               T(f"Geprüft: {scope} – {len(files)} Dateien.", f"Checked: {scope} – {len(files)} files."), ""]
        if unverified is not None:
            out += [T("⏹ **Vorzeitig gestoppt** – hier der Stand bis dahin. Nichts wird automatisch behoben; mit "
                      "/review geht es von vorn los.", "⏹ **Stopped early** – this is the state so far. Nothing is "
                      "fixed automatically; /review starts over."), ""]
            if checks:
                out += [f"**{T('Prüfungen', 'Checks')}**", *[self._check_line(c) for c in checks], ""]
            if unverified:
                out.append(f"**{T('Mögliche Funde – nicht gegengeprüft', 'Possible findings – not verified')} "
                           f"({len(unverified)})**")
                out += [self._finding_line(f, en) for f in unverified]
            else:
                out.append(T("Bis dahin keine möglichen Funde.", "No possible findings so far."))
            return "\n".join(out).strip()
        if checks:
            out += [f"**{T('Prüfungen', 'Checks')}**", *[self._check_line(c) for c in checks], ""]
        if verified:
            out.append(f"**{T('Bestätigte Funde', 'Confirmed findings')} ({len(verified)})**")
            out += [self._finding_line(f, en) for f in verified]
            out.append("")
        else:
            out += [T("Keine bestätigten Funde.", "No confirmed findings."), ""]
        if total > len(verified):
            out.append(T(f"{total - len(verified)} weitere mögliche Funde ließen sich nicht belegen und sind verworfen.",
                         f"{total - len(verified)} further possible findings could not be proven and were dropped."))
        failing = [c for c in checks if c["status"] == "fail"]
        todo = [f for f in verified if f["severity"] in ("high", "medium")]
        if failing or todo:
            out.append(T("Ich behebe das jetzt in der Sandbox und prüfe danach erneut.",
                         "I am fixing this in the sandbox now and will check again afterwards."))
        return "\n".join(out).strip()

    @staticmethod
    def _finding_line(f: dict, en: bool) -> str:
        where = f"`{f['file']}:{f['line']}`" if f["line"] else f"`{f['file']}`"
        focus = FOCUS_LABEL[f["focus"]][1 if en else 0]
        return (f"- {SEV_ICON[f['severity']]} {where} · {focus} – **{f['title']}**  \n  {f['why']}"
                + (f"  \n  → {f['fix']}" if f["fix"] else ""))

    def _fix_prompt(self, checks: list[dict], failing: list[dict], todo: list[dict]) -> str:
        en = self.cfg.language == "en"
        commands = "\n".join(f"- `{c['cmd']}`" for c in checks if c["status"] != "skip" and c["cmd"]
                             and not c["cmd"].startswith("check_")) or T("- (die Tests des Projekts)",
                                                                         "- (the project's tests)")
        if any(c["cmd"].startswith("check_") for c in checks if c["status"] != "skip"):
            commands += T("\n- die Sicherheits-Scanner (bandit/semgrep/gitleaks) auf den geänderten Dateien",
                          "\n- the security scanners (bandit/semgrep/gitleaks) on the changed files")
        parts = []
        for c in failing:
            parts.append(f"### ✘ {review_checks.label(c['name'], en)}\n`{c['cmd']}`\n```\n{c['output'][-2500:]}\n```")
        for i, f in enumerate(todo, 1):
            where = f"{f['file']}:{f['line']}" if f["line"] else f["file"]
            parts.append(f"{i}. [{f['severity']}] {where} – {f['title']}: {f['why']}"
                         + (f" → {f['fix']}" if f["fix"] else ""))
        text = prompts.review_text(self.cfg, "fix").format(commands=commands, findings="\n\n".join(parts))
        return text if est_tokens(text) < self.context_budget() * 0.3 else text[: int(self.context_budget() * 0.3 * 3)]
