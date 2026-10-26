"""Kontext-Stufen: Wie viel Platz bekommt was im Prompt – abhängig von der Größe des Kontextfensters.

Unter 16k Token (z. B. 8k auf einer 8-GB-Karte) wird gespart: wenige Werkzeuge vorab (der Rest kommt per
Stichwort oder load_tools), kürzere Werkzeug-Ergebnisse, weniger Erinnerungen und Fakten, kurze Zusammenfassungen.
Ab 16k bleibt für alles mehr Raum. In beiden Stufen werden alte Werkzeug-Ergebnisse ausgeblendet, bevor
zusammengefasst wird (wie Claude Code) – das kostet keinen Modellaufruf. Ein Viertel des Fensters (höchstens 16k)
bleibt immer für die Ausgabe frei – sonst bricht eine lange Antwort oder ein write mit einer ganzen Datei mitten im
Aufruf ab.
"""

from __future__ import annotations

from dataclasses import dataclass

SMALL_BELOW = 16384  # darunter: kleines Fenster (Sparmodus)

ANSWER_RESERVE = 1500  # Token, die im Kontextfenster für die Antwort frei bleiben
THINK_RESERVE = 3000  # mit Denkmodus: die Denkkette belegt dasselbe Fenster
OUTPUT_SHARE = 0.25  # so viel vom Fenster bleibt für die Ausgabe frei …
OUTPUT_RESERVE_MAX = 16000  # … höchstens so viele Token
SUMMARY_MIN = 500  # kürzeste Zusammenfassung im großen Fenster, wenn der Platz knapp ist


def _clamp(value: float, low: int, high: int) -> int:
    return int(max(low, min(high, value)))


@dataclass(frozen=True)
class ContextPlan:
    window: int
    small: bool
    answer_reserve: int  # Platz für die Antwort (ohne Denken)
    think_cap: int  # Höchstlänge der Denkkette (0 = wie die Stufe sagt, „gründlich“ unbegrenzt)
    output_share: float  # so viel vom Fenster darf ein Werkzeug-Ergebnis belegen
    tool_share: float  # weiche Obergrenze der Werkzeugbeschreibungen (Anteil am Prompt-Budget)
    keep_share: float  # beim Ausblenden bleiben die neuesten Ergebnisse bis zu diesem Anteil ungekürzt
    stub_chars: int  # so viel vom Anfang eines ausgeblendeten Ergebnisses bleibt stehen
    memories: int  # Erinnerungen in der ersten Frage einer Epoche (höchstens; Config kann weniger sagen)
    facts: int  # dauerhafte Fakten im Systemprompt (höchstens)
    summary_max: int  # Länge der Zusammenfassung, wenn Platz ist
    summary_floor: int  # so viel muss beim Komprimieren mindestens frei sein
    summary_min: int  # kürzeste Zusammenfassung, wenn es eng ist
    instruction_reserve: int  # Platz für die Komprimierungs-Anweisung
    carry_share: float  # so viel dürfen die wörtlich mitgenommenen letzten Schritte belegen
    appendix_users: int  # Nutzernachrichten, die der Zusammenfassung angehängt werden …
    appendix_chars: int  # … je höchstens so lang
    appendix_paths: int  # berührte Dateien/Ordner im Anhang

    @property
    def name(self) -> str:
        return "small" if self.small else "large"

    def output_chars(self) -> int:
        """Höchstlänge eines Werkzeug-Ergebnisses in Zeichen (≈ 3 Zeichen pro Token)."""
        return max(1500, int(self.window * self.output_share * 3))

    def think_budget(self) -> int:
        """Höchstlänge der Denkkette – nur im kleinen Fenster begrenzt (sonst passt die Antwort nicht mehr hinein);
        wie lange gedacht wird, bestimmt sonst das Modell bzw. seine eigene Stufe (0 = unbegrenzt)."""
        return self.think_cap

    def reserve(self, think: bool) -> int:
        """Freier Platz für Antwort (und Denkkette): mindestens ein Viertel des Fensters (höchstens 16k)."""
        if not think:
            base = self.answer_reserve
        elif self.small:
            base = self.answer_reserve + self.think_budget()
        else:
            base = THINK_RESERVE
        return max(base, min(OUTPUT_RESERVE_MAX, int(self.window * OUTPUT_SHARE)))


def plan_for(window: int) -> ContextPlan:
    """Stufe und Größen für ein Kontextfenster (in Token)."""
    window = int(window)
    if window < SMALL_BELOW:
        return ContextPlan(
            window=window, small=True, answer_reserve=1024, think_cap=int(window * 0.2),
            output_share=0.10, tool_share=0.35, keep_share=0.12, stub_chars=200,
            memories=int(window * 0.06), facts=int(window * 0.05),
            summary_max=_clamp(window * 0.08, 400, 900), summary_floor=_clamp(window * 0.05, 300, 600),
            summary_min=300, instruction_reserve=300, carry_share=0.15,
            appendix_users=5, appendix_chars=120, appendix_paths=6)
    return ContextPlan(
        window=window, small=False, answer_reserve=ANSWER_RESERVE, think_cap=0,
        output_share=0.15, tool_share=0.50, keep_share=0.25, stub_chars=300,
        memories=int(window * 0.08), facts=int(window * 0.06),
        summary_max=_clamp(window * 0.1, 1200, 4000), summary_floor=_clamp(window * 0.06, 900, 2500),
        summary_min=SUMMARY_MIN, instruction_reserve=400, carry_share=0.20,
        appendix_users=8, appendix_chars=160, appendix_paths=12)
