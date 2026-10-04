"""Bilder erzeugen aus dem Tools-Modus heraus („mal mir ein Bild von …“) – dasselbe Backend wie der Bild-Modus
(imagegen.py). Bestätigung, weil dafür auf kleinen Karten das Sprachmodell kurz aus dem Grafikspeicher muss."""

from __future__ import annotations

from typing import Annotated, Any

from ..lang import T
from .registry import CONFIRM, ToolContext, tool


def _enabled(cfg: Any) -> bool:
    from ..imagegen_setup import image_dir, is_set_up
    return is_set_up(image_dir(cfg))


@tool("Erzeugt ein Bild mit dem lokalen Bildmodell (Qwen-Image-2.1) aus einer Beschreibung. Den Prompt am besten "
      "auf Englisch und konkret formulieren (Motiv, Stil, Licht, Perspektive). Dauert je nach Grafikkarte 1–3 Minuten.",
      risk=CONFIRM, enabled=_enabled, editable=("prompt",))
async def generate_image(
    ctx: ToolContext,
    prompt: Annotated[str, "Bildbeschreibung (am besten Englisch)"],
    size: Annotated[str, "Optional: Format 1:1, 4:3, 3:4, 16:9 oder 9:16"] = "",
    negative: Annotated[str, "Optional: was nicht aufs Bild soll"] = "",
) -> str:
    from ..imagegen import ImageError
    engine = ctx.services.get("images")
    if engine is None:
        return T("Der Bild-Modus läuft nur mit dem Orbwise-Server.", "Image generation needs the Orbwise server.")

    async def progress(state: dict) -> None:
        if state.get("steps"):
            await ctx.output(T(f"Schritt {state['step']}/{state['steps']}\n", f"step {state['step']}/{state['steps']}\n"))
    try:
        saved = await engine.generate(prompt, negative=negative, size=size, progress=progress)
    except ImageError as e:
        return str(e)
    img = saved[0]
    return T(f"Bild gespeichert: {img['path']} ({img['width']}×{img['height']}, Seed {img['seed']}, {img['seconds']} s). "
             "Es steht auch im Bild-Modus in der Galerie; zum Ansehen open_file nutzen.",
             f"Image saved: {img['path']} ({img['width']}×{img['height']}, seed {img['seed']}, {img['seconds']} s). "
             "It is also in the image mode gallery; use open_file to show it.")
