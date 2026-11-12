"""Lange Texte für das Modell zuschneiden: in Passagen teilen und die zur Frage passendsten auswählen
(Stichwort-Treffer + optional Embedding-Ähnlichkeit). Genutzt von obsidian_ask."""

from __future__ import annotations

import math
import re

PASSAGE_CHARS = 1200


def split_passages(text: str, size: int = PASSAGE_CHARS) -> list[str]:
    """Text in Passagen ~size Zeichen, an Absatz-/Satzgrenzen, mit etwas Überlappung."""
    text = re.sub(r"[ \t]+", " ", text or "").strip()
    if len(text) <= size:
        return [text] if text else []
    out, start = [], 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            cut = max(text.rfind("\n\n", start, end), text.rfind(". ", start + size // 2, end))
            if cut > start + size // 3:
                end = cut + 1
        out.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(end - size // 8, start + 1)  # ~150 Zeichen Überlappung
    return [p for p in out if p]


STOPWORDS = set("""aber alle als also auch auf aus bei bin bis bitte dann das dass dem den der des die dies diese
dieser doch dort durch ein eine einem einen einer eines für hab habe hat hier ich ihr ihre ist jetzt kann kein
mein meine meinem meinen meiner mich mir mit muss nach nicht noch nur oder schon sein sich sie sind so über um und
uns unter vom von vor war was wann warum weil welche welcher wenn wer wie wieviel wird wo zu zum zur""".split())


def _words(text: str) -> set[str]:
    # grobe Stammform: erste 6 Zeichen, ohne Füllwörter
    return {w[:6] for w in re.findall(r"\w{3,}", text.lower()) if w not in STOPWORDS}


def keyword_scores(question: str, passages: list[str]) -> list[float]:
    q = _words(question)
    scores = []
    for p in passages:
        low = p.lower()
        scores.append(sum(1 + math.log1p(low.count(w)) for w in q if w in low))
    return scores


def _normalize(values: list[float]) -> list[float]:
    lo, hi = min(values), max(values)
    return [0.0] * len(values) if hi - lo < 1e-9 else [(v - lo) / (hi - lo) for v in values]


async def rank_passages(question: str, passages: list[str], embedder=None) -> list[int]:
    """Indizes der Passagen, relevanteste zuerst: Stichwort-Treffer und (falls verfügbar) Embedding-Ähnlichkeit,
    jeweils auf 0…1 normiert und gemittelt – ein eindeutiger Stichwort-Treffer geht so nicht im Rauschen unter."""
    score = _normalize(keyword_scores(question, passages))
    if embedder is not None:
        try:
            vecs = await embedder([question, *passages])
            qv = vecs[0]
            qn = math.sqrt(sum(x * x for x in qv)) or 1.0

            def cos(v):
                return sum(a * b for a, b in zip(qv, v)) / (qn * (math.sqrt(sum(x * x for x in v)) or 1.0))

            sims = _normalize([cos(v) for v in vecs[1:]])
            score = [(a + b) / 2 for a, b in zip(score, sims)]
        except Exception:  # noqa: BLE001 – ohne Embeddings (Modell fehlt) reicht die Stichwortsuche
            pass
    return sorted(range(len(passages)), key=lambda i: -score[i])


def select_passages(passages: list[str], order: list[int], budget: int) -> list[int]:
    """So viele der bestplatzierten Passagen, wie ins Zeichenbudget passen (mindestens eine), in Textreihenfolge."""
    chosen, used = [], 0
    for i in order:
        if used + len(passages[i]) > budget and chosen:
            break
        chosen.append(i)
        used += len(passages[i])
    return sorted(chosen)
