"""Tool-Registry: Funktionen werden per Decorator registriert, das JSON-Schema für Ollama
wird aus den Typannotationen (Annotated[typ, "Beschreibung"]) erzeugt."""

from __future__ import annotations

import inspect
import json
import logging
import os
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, get_args, get_origin

log = logging.getLogger(__name__)

SAFE, CONFIRM, BLOCKED = "safe", "confirm", "blocked"

RiskFn = Callable[["ToolContext", dict], tuple[str, str]]


@dataclass
class ToolContext:
    cfg: Any
    memory: Any
    emit: Callable[[dict], Awaitable[None]] | None = None
    call_id: str = ""
    # Gemeinsame Dienste des Servers (z. B. "reminders": ReminderStore)
    services: dict = field(default_factory=dict)
    # Arbeitsordner (Coding-Modus: Projektordner des Chats) – Shell startet dort, relative Pfade beziehen sich darauf
    cwd: str | None = None
    # Höchstlänge eines Ergebnisses nach dem Kontextfenster (vom Agenten gesetzt, siehe context_plan.py)
    output_chars: int | None = None

    def path(self, raw: str) -> Path:
        p = Path(os.path.expandvars(str(raw or ""))).expanduser()
        return p if p.is_absolute() or not self.cwd else Path(self.cwd) / p

    def limit(self, cap: int | None = None) -> int:
        """Wie viele Zeichen eines Ergebnisses in den Prompt dürfen: der Config-Wert (tools.max_output_chars bzw.
        cap, z. B. paperless.max_chars), bei kleinem Kontextfenster weniger – längere Ausgaben landen vollständig in
        einer Datei (proc.clip_saved) oder werden abschnittsweise gelesen (offset)."""
        cap = cap or self.cfg.tools.max_output_chars
        return min(cap, self.output_chars) if self.output_chars else cap

    async def output(self, text: str) -> None:
        """Live-Ausgabe eines laufenden Tools an die Oberfläche."""
        if self.emit:
            await self.emit({"type": "tool_output", "id": self.call_id, "text": text})


@dataclass
class ToolSpec:
    name: str
    description: str
    func: Callable[..., Awaitable[str]]
    parameters: dict
    risk: str | RiskFn = SAFE
    param_types: dict[str, type] = field(default_factory=dict)
    enabled: Callable[[Any], bool] | None = None
    group: str = ""  # Modulname, z. B. "sysadmin" – ganze Gruppen lassen sich per tools.disabled abschalten
    # Parameter, die der Nutzer im Bestätigungsfenster noch ändern darf (z. B. Empfänger/Betreff/Text einer Mail)
    editable: tuple[str, ...] = ()
    # Beschreibung, die von der Konfiguration abhängt (z. B. welche Gruppen load_tools nachladen kann)
    describe: Callable[[Any], str] | None = None

    def is_enabled(self, cfg: Any) -> bool:
        if cfg is None:
            return True
        disabled = set(getattr(getattr(cfg, "tools", None), "disabled", None) or [])
        if self.name in disabled or self.group in disabled:
            return False
        return self.enabled is None or bool(self.enabled(cfg))

    def schema(self, cfg: Any = None) -> dict:
        description = self.describe(cfg) if self.describe and cfg is not None else self.description
        return {"type": "function",
                "function": {"name": self.name, "description": description,
                             "parameters": lean_parameters(self.parameters)}}

    def assess(self, ctx: ToolContext, args: dict) -> tuple[str, str]:
        if callable(self.risk):
            return self.risk(ctx, args)
        return self.risk, ""


def lean_parameters(parameters: dict) -> dict:
    """Für den Prompt ohne Ballast: „Optional: “ vor Beschreibungen sagt schon „required“, leeres required fällt
    weg. Namen, Typen und Pflichtangaben bleiben unverändert."""
    props = {}
    for name, prop in parameters.get("properties", {}).items():
        prop = dict(prop)
        desc = prop.get("description") or ""
        if desc.startswith("Optional: "):
            desc = desc[len("Optional: "):]
            prop["description"] = desc[:1].upper() + desc[1:]
        props[name] = prop
    out = {"type": "object", "properties": props}
    if parameters.get("required"):
        out["required"] = list(parameters["required"])
    return out


REGISTRY: dict[str, ToolSpec] = {}

_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _json_type(tp: Any) -> tuple[str, type]:
    origin = get_origin(tp)
    if origin in (typing.Union, getattr(__import__("types"), "UnionType", None)):
        inner = [a for a in get_args(tp) if a is not type(None)]
        return _json_type(inner[0])
    if origin is list:
        return "array", list
    return _JSON_TYPES.get(tp, "string"), tp if tp in _JSON_TYPES else str


def tool(description: str, risk: str | RiskFn = SAFE, name: str | None = None,
         enabled: Callable[[Any], bool] | None = None, editable: tuple[str, ...] = (),
         describe: Callable[[Any], str] | None = None):
    def deco(func):
        sig = inspect.signature(func)
        hints = typing.get_type_hints(func, include_extras=True)
        props, required, types = {}, [], {}
        for pname, param in list(sig.parameters.items())[1:]:  # erstes Argument ist ctx
            hint = hints.get(pname, str)
            desc = ""
            if get_origin(hint) is Annotated:
                hint, desc = get_args(hint)[0], get_args(hint)[1]
            jtype, pytype = _json_type(hint)
            prop: dict[str, Any] = {"type": jtype}
            if jtype == "array":
                item = (get_args(hint) or (str,))[0]
                is_obj = get_origin(item) is dict or item is dict
                prop["items"] = {"type": "object" if is_obj else _JSON_TYPES.get(item, "string")}
            if desc:
                prop["description"] = desc
            props[pname] = prop
            types[pname] = pytype
            if param.default is inspect.Parameter.empty:
                required.append(pname)
        spec = ToolSpec(
            name=name or func.__name__,
            description=description,
            func=func,
            parameters={"type": "object", "properties": props, "required": required},
            risk=risk,
            param_types=types,
            enabled=enabled,
            group=func.__module__.rsplit(".", 1)[-1],
            editable=tuple(editable),
            describe=describe,
        )
        REGISTRY[spec.name] = spec
        return func
    return deco


def coerce_args(spec: ToolSpec, raw: Any) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            raw = {}
    raw = raw if isinstance(raw, dict) else {}
    args = {}
    for key, value in raw.items():
        tp = spec.param_types.get(key)
        if tp is None:
            continue  # unbekannte Parameter ignorieren (LLM halluziniert gelegentlich)
        try:
            if tp is bool and isinstance(value, str):
                value = value.strip().lower() in ("true", "1", "ja", "yes")
            elif tp in (int, float) and not isinstance(value, bool):
                value = tp(value)
            elif tp is str and not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
        except (TypeError, ValueError):
            continue
        args[key] = value
    return args


def missing_args(spec: ToolSpec, args: dict) -> list[str]:
    return [p for p in spec.parameters["required"] if p not in args]


def tool_schemas(cfg: Any = None) -> list[dict]:
    """Schemas aller Tools; mit cfg nur die aktivierten (z. B. Trilium nur, wenn konfiguriert)."""
    return [s.schema(cfg) for s in REGISTRY.values() if s.is_enabled(cfg)]


def get_tool(name: str) -> ToolSpec | None:
    return REGISTRY.get(name)


def load_all_tools() -> dict[str, ToolSpec]:
    # Import registriert die Tools per Decorator
    from . import (  # noqa: F401
        apps,
        briefing,
        calendar_tools,
        files,
        homeassistant,
        image_tools,
        mail,
        memory_tools,
        obsidian,
        packages,
        paperless,
        portainer,
        power,
        reminder_tools,
        routine_tools,
        shell,
        ssh,
        sysadmin,
        system,
        telegram_tools,
        todo_tools,
        toolload,
        trilium,
        vision,
        weather,
        web,
    )
    return REGISTRY
