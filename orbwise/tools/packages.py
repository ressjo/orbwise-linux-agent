"""Paketverwaltung: Arch/Manjaro (pacman + optional AUR-Helper yay/paru) und Debian/Ubuntu (apt)."""

from __future__ import annotations

import re
import shutil
from typing import Annotated

from ..lang import T
from . import proc
from .registry import CONFIRM, SAFE, ToolContext, tool

PKG_RE = re.compile(r"^[a-z0-9@._+-]+$")


def privileged(ctx: ToolContext, argv: list[str]) -> list[str]:
    mode = ctx.cfg.tools.privilege_cmd
    if mode == "sudo":
        return ["sudo", "-n", *argv]
    if mode == "pkexec":
        return ["pkexec", *argv]
    return ["sudo", "-A", *argv]  # "dashboard": Passwortdialog in der Oberfläche


def package_manager(ctx: ToolContext) -> str:
    """'pacman' oder 'apt' – aus der Config (tools.package_manager) oder automatisch erkannt."""
    choice = getattr(ctx.cfg.tools, "package_manager", "auto")
    if choice in ("pacman", "apt"):
        return choice
    if shutil.which("pacman"):
        return "pacman"
    if shutil.which("apt-get"):
        return "apt"
    return "pacman"


APT_ENV = ["env", "DEBIAN_FRONTEND=noninteractive"]


def aur_helper(ctx: ToolContext) -> str | None:
    if package_manager(ctx) != "pacman":
        return None
    choice = ctx.cfg.tools.aur_helper
    if choice == "none":
        return None
    if choice in ("yay", "paru"):
        return choice if shutil.which(choice) else None
    return next((h for h in ("yay", "paru") if shutil.which(h)), None)


def helper_cmd(ctx: ToolContext, helper: str, *args: str) -> list[str]:
    # AUR-Helper laufen als normaler Nutzer und holen sich Root-Rechte selbst
    mode = ctx.cfg.tools.privilege_cmd
    if mode == "pkexec":
        priv = ["--sudo", "pkexec"]
    elif mode == "sudo":
        priv = ["--sudoflags", "-n"]
    else:
        priv = ["--sudoflags", "-A"]
    unattended = (["--answerclean", "None", "--answerdiff", "None", "--answeredit", "None"]
                  if helper == "yay" else ["--skipreview"])
    return [helper, *priv, *args, "--noconfirm", *unattended]


def split_names(names: str) -> list[str]:
    return [n for n in re.split(r"[\s,]+", names.strip().lower()) if n]


def _names_risk(verb):  # verb: () -> Text in der aktuellen Sprache
    def risk(ctx: ToolContext, args: dict) -> tuple[str, str]:
        pkgs = split_names(args.get("names", ""))
        bad = [p for p in pkgs if not PKG_RE.match(p)]
        if not pkgs or bad:
            return SAFE, T("ungültige Paketnamen werden abgelehnt", "invalid package names are rejected")
        return CONFIRM, f"{verb()}: {', '.join(pkgs)}"
    return risk


@tool("Aktualisiert das ganze System (pacman -Syu + AUR bzw. apt-get upgrade).",
      risk=lambda ctx, a: (CONFIRM, T("Systemupdate mit Root-Rechten", "system update with root privileges")))
async def system_update(ctx: ToolContext) -> str:
    limit = ctx.limit()
    timeout = ctx.cfg.tools.update_timeout
    if package_manager(ctx) == "apt":
        rc, out = await proc.run(ctx, privileged(ctx, [*APT_ENV, "sh", "-c", "apt-get update && apt-get -y upgrade"]),
                                 timeout=timeout)
        result = "apt-get upgrade: " + proc.format_result(rc, out, limit)
        if re.search(r"linux-image-\S+", out) and "upgraded" in out:
            result += "\n\nHinweis: Möglicherweise wurde der Kernel aktualisiert – ein Neustart wird empfohlen."
        return result
    rc, out = await proc.run(ctx, privileged(ctx, ["pacman", "-Syu", "--noconfirm"]), timeout=timeout)
    result = "pacman -Syu: " + proc.format_result(rc, out, limit // 2)
    helper = aur_helper(ctx)
    if rc == 0 and helper:
        rc2, out2 = await proc.run(ctx, helper_cmd(ctx, helper, "-Sua"), timeout=timeout)
        result += f"\n\n{helper} -Sua: " + proc.format_result(rc2, out2, limit // 2)
    if re.search(r"(upgrading|aktualisiere|Aktualisiere)\s+linux(-lts|-zen|-hardened)?[\s.]", out):
        result += "\n\nHinweis: Der Kernel wurde aktualisiert – ein Neustart wird empfohlen."
    return result


@tool("Listet verfügbare Updates auf, ohne etwas zu installieren.")
async def list_updates(ctx: ToolContext) -> str:
    if package_manager(ctx) == "apt":
        rc, out = await proc.run(ctx, ["apt", "list", "--upgradable"], timeout=120, stream=False)
        lines = [ln for ln in out.splitlines() if "/" in ln and not ln.startswith(("Listing", "Auflistung", "WARNING"))]
        return proc.clip("\n".join(lines) or "Keine Updates verfügbar (Paketlisten ggf. veraltet – system_update "
                         "aktualisiert sie).", ctx.limit())
    if shutil.which("checkupdates"):
        rc, out = await proc.run(ctx, ["checkupdates"], timeout=120, stream=False)
        if rc == 2:
            out = "Keine Updates verfügbar."
    else:
        rc, out = await proc.run(ctx, ["pacman", "-Qu"], timeout=60, stream=False)
    helper = aur_helper(ctx)
    if helper:
        _, aur = await proc.run(ctx, [helper, "-Qua"], timeout=120, stream=False)
        if aur.strip():
            out += f"\nAUR:\n{aur}"
    return proc.clip(out.strip() or "Keine Updates verfügbar.", ctx.limit())


@tool("Sucht Pakete in den Paketquellen (Arch: auch AUR).")
async def search_package(ctx: ToolContext, query: str) -> str:
    if package_manager(ctx) == "apt":
        rc, out = await proc.run(ctx, ["apt-cache", "search", "--", query], timeout=60, stream=False)
        return "Pakete:\n" + ("\n".join(out.splitlines()[:40]) if out.strip() else "(nichts gefunden)")
    rc, out = await proc.run(ctx, ["pacman", "-Ss", query], timeout=60, stream=False)
    result = "Repositories:\n" + ("\n".join(out.splitlines()[:30]) if out.strip() else "(nichts gefunden)")
    helper = aur_helper(ctx)
    if helper:
        _, aur = await proc.run(ctx, [helper, "-Ss", "--aur", query], timeout=60, stream=False)
        result += "\n\nAUR:\n" + ("\n".join(aur.splitlines()[:20]) if aur.strip() else "(nichts gefunden)")
    return result


@tool("Installiert Pakete (Arch notfalls aus dem AUR). Namen exakt – im Zweifel erst search_package.",
      risk=_names_risk(lambda: T("Pakete installieren", "install packages")))
async def install_package(ctx: ToolContext, names: Annotated[str, "Mit Leerzeichen getrennt"]) -> str:
    pkgs = split_names(names)
    invalid = [p for p in pkgs if not PKG_RE.match(p)]
    if not pkgs or invalid:
        return f"Ungültige Paketnamen: {', '.join(invalid) or '(leer)'}"
    if package_manager(ctx) == "apt":
        found, missing = [], []
        for p in pkgs:
            rc, out = await proc.run(ctx, ["apt-cache", "show", p], timeout=30, stream=False)
            (found if rc == 0 and out.strip() else missing).append(p)
        parts = []
        if found:
            rc, out = await proc.run(ctx, privileged(ctx, [*APT_ENV, "apt-get", "-y", "install", *found]),
                                     timeout=ctx.cfg.tools.update_timeout)
            parts.append(f"apt-get install {' '.join(found)}: " + proc.format_result(rc, out, ctx.limit()))
        if missing:
            parts.append(f"Nicht gefunden: {', '.join(missing)}")
        return "\n\n".join(parts)
    repo, aur, missing = [], [], []
    helper = aur_helper(ctx)
    for p in pkgs:
        rc, _ = await proc.run(ctx, ["pacman", "-Si", p], timeout=30, stream=False)
        if rc == 0:
            repo.append(p)
        elif helper:
            rc, _ = await proc.run(ctx, [helper, "-Si", "--aur", p], timeout=60, stream=False)
            (aur if rc == 0 else missing).append(p)
        else:
            missing.append(p)
    parts = []
    limit = ctx.limit()
    if repo:
        rc, out = await proc.run(ctx, privileged(ctx, ["pacman", "-S", "--needed", "--noconfirm", *repo]),
                                 timeout=ctx.cfg.tools.update_timeout)
        parts.append(f"pacman -S {' '.join(repo)}: " + proc.format_result(rc, out, limit // 2))
    if aur and helper:
        rc, out = await proc.run(ctx, helper_cmd(ctx, helper, "-S", "--needed", *aur),
                                 timeout=ctx.cfg.tools.update_timeout)
        parts.append(f"{helper} -S {' '.join(aur)}: " + proc.format_result(rc, out, limit // 2))
    if missing:
        parts.append(f"Nicht gefunden: {', '.join(missing)}")
    return "\n\n".join(parts)


@tool("Deinstalliert Pakete samt nicht mehr benötigter Abhängigkeiten.",
      risk=_names_risk(lambda: T("Pakete entfernen", "remove packages")))
async def remove_package(ctx: ToolContext, names: Annotated[str, "Mit Leerzeichen getrennt"]) -> str:
    pkgs = split_names(names)
    if not pkgs or any(not PKG_RE.match(p) for p in pkgs):
        return "Ungültige Paketnamen."
    argv = ([*APT_ENV, "apt-get", "-y", "remove", "--autoremove", *pkgs] if package_manager(ctx) == "apt"
            else ["pacman", "-Rns", "--noconfirm", *pkgs])
    rc, out = await proc.run(ctx, privileged(ctx, argv), timeout=ctx.cfg.tools.shell_timeout * 5)
    return proc.format_result(rc, out, ctx.limit())
