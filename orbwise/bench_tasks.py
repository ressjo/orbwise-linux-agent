"""Aufgaben des Agent-Benchmarks (`orbwise bench`): kleine Wegwerf-Projekte, je eine Bitte an den Coding-Agenten und
eine Prüfung, die objektiv feststellt, ob sie erfüllt ist.

Jede Aufgabe bringt eine Musterlösung mit (solution/answer) – die Tests prüfen damit, dass die Prüfung im
Ausgangszustand „nicht gelöst“ und mit der Lösung „gelöst“ sagt. Dateien unter `protect` setzt die Prüfung vorher auf
ihren Ausgangsstand zurück: Tests abschwächen, damit sie grün werden, zählt nicht.

Die Aufgaben decken ab, woran ein Agent mit einem lokalen Modell typischerweise scheitert: Fehler finden statt raten,
genaue Änderungen in großen Dateien, mehrere Dateien anfassen, nur lesen und antworten, neue Dateien anlegen, das
Ergebnis durch Ausführen prüfen.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

Check = Callable[[Path, str], "tuple[bool, str]"]  # (Projektordner, Antwort des Agenten) → (gelöst?, Grund)


@dataclass(frozen=True)
class Task:
    name: str
    title: tuple[str, str]                 # (deutsch, englisch) – was geprüft wird, für den Bericht
    prompt: tuple[str, str]                # die Bitte an den Agenten
    files: dict[str, str]                  # Ausgangszustand des Projekts
    check: Check
    protect: tuple[str, ...] = ()          # vor der Prüfung auf den Ausgangsstand zurücksetzen
    solution: dict[str, str] = field(default_factory=dict)  # Musterlösung (Dateien) – für die Tests der Aufgaben
    answer: str = ""                       # Musterantwort, wenn die Aufgabe eine Auskunft verlangt
    read_only: bool = False                # die Aufgabe darf nichts ändern


def pytest_ok(project: Path, timeout: int = 120) -> tuple[bool, str]:
    """Laufen die Tests des Projekts durch? → (ja?, letzte Zeile der Ausgabe)."""
    try:
        r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-x", "-p", "no:cacheprovider"], cwd=project,
                           capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f"pytest: {type(e).__name__}"
    lines = [ln for ln in (r.stdout + r.stderr).strip().splitlines() if ln.strip()]
    return r.returncode == 0, lines[-1][:160] if lines else f"pytest rc={r.returncode}"


def _tests(project: Path, answer: str) -> tuple[bool, str]:  # noqa: ARG001
    return pytest_ok(project)


# ---------------------------------------------------------------- 1 Fehler finden
_STATS = '''def mean(values):
    return sum(values) / len(values)


def median(values):
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 0:
        return ordered[mid]
    return ordered[mid]


def spread(values):
    return max(values) - min(values)
'''
_STATS_TEST = '''from stats import mean, median, spread


def test_mean():
    assert mean([1, 2, 3]) == 2


def test_median_odd():
    assert median([5, 1, 3]) == 3


def test_median_even():
    assert median([4, 1, 3, 2]) == 2.5


def test_spread():
    assert spread([2, 9, 4]) == 7
'''

# ---------------------------------------------------------------- 2 Funktion ergänzen
_TEXT = '''def shout(text):
    return text.upper() + "!"
'''
_TEXT_TEST = '''from text import shout, slugify


def test_shout():
    assert shout("hi") == "HI!"


def test_slugify_basic():
    assert slugify("Hello World") == "hello-world"


def test_slugify_strips_punctuation_and_spaces():
    assert slugify("  Was ist das?!  ") == "was-ist-das"


def test_slugify_collapses_separators():
    assert slugify("a  --  b") == "a-b"
'''
_TEXT_SOLVED = _TEXT + '''

def slugify(text):
    import re
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
'''

# ---------------------------------------------------------------- 3 umbenennen über mehrere Dateien
_CART = '''def calc(items):
    return sum(price * count for price, count in items)
'''
_ORDER = '''from cart import calc


def order_total(items, shipping):
    return calc(items) + shipping
'''
_REPORT = '''import cart


def summary(items):
    return f"Summe: {cart.calc(items)}"
'''
_CART_TEST = '''from cart import compute_total
from order import order_total
from report import summary


def test_compute_total():
    assert compute_total([(2, 3), (1, 4)]) == 10


def test_order_total():
    assert order_total([(2, 3)], 4) == 10


def test_summary():
    assert summary([(5, 2)]) == "Summe: 10"
'''


def _renamed(project: Path, answer: str) -> tuple[bool, str]:  # noqa: ARG001
    for name in ("cart.py", "order.py", "report.py"):
        if re.search(r"\bcalc\b", (project / name).read_text(encoding="utf-8")):
            return False, f"{name}: calc kommt noch vor"
    return pytest_ok(project)


# ---------------------------------------------------------------- 4 ein Wert an mehreren Stellen
_CONFIG = '''HOST = "127.0.0.1"
PORT = 8000
DEBUG = False
'''
_SETTINGS = '{\n  "name": "demo",\n  "port": 8000,\n  "workers": 2\n}\n'
_README = "# Demo\n\nStart mit `python server.py`, dann http://127.0.0.1:8000 öffnen.\n\nDer Port 8000 steht in config.py.\n"


def _port(project: Path, answer: str) -> tuple[bool, str]:  # noqa: ARG001
    try:
        settings = json.loads((project / "settings.json").read_text(encoding="utf-8"))
    except ValueError:
        return False, "settings.json ist kein gültiges JSON mehr"
    if settings.get("port") != 8080 or settings.get("workers") != 2:
        return False, "settings.json: port ist nicht 8080 (oder etwas anderes wurde geändert)"
    for name in ("config.py", "README.md"):
        text = (project / name).read_text(encoding="utf-8")
        if "8000" in text or "8080" not in text:
            return False, f"{name}: 8000 steht noch da bzw. 8080 fehlt"
    return True, "alle drei Stellen geändert"


# ---------------------------------------------------------------- 5 nur lesen und antworten
_PRICING = '''TAX = 0.19


def net(price):
    return round(price / (1 + TAX), 2)


def apply_discount(price, customer):
    rate = 0.1 if customer.get("vip") else 0.0
    return round(price * (1 - rate), 2)
'''
_CHECKOUT = '''from pricing import apply_discount, net


def checkout(price, customer):
    final = apply_discount(price, customer)
    return {"final": final, "net": net(final)}
'''
_INVOICE = '''def render(order):
    return f"Rechnung: {order['final']:.2f} EUR (netto {order['net']:.2f})"
'''


def _found(project: Path, answer: str) -> tuple[bool, str]:  # noqa: ARG001
    ok = "apply_discount" in answer and "pricing" in answer
    return ok, "nennt pricing.py und apply_discount" if ok else "Datei oder Funktion fehlt in der Antwort"


# ---------------------------------------------------------------- 6 neues Skript
_SAMPLE = "eins zwei drei\nvier  fünf\n\nsechs sieben\n"


def _wordcount(project: Path, answer: str) -> tuple[bool, str]:  # noqa: ARG001
    script = project / "wordcount.py"
    if not script.is_file():
        return False, "wordcount.py fehlt"
    try:
        r = subprocess.run([sys.executable, "wordcount.py", "sample.txt"], cwd=project, capture_output=True, text=True,
                           timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, type(e).__name__
    return r.stdout.strip() == "7", f"Ausgabe: {r.stdout.strip()[:60]!r} (erwartet '7')"


# ---------------------------------------------------------------- 7 Syntaxfehler
_APP_BROKEN = '''import json


def load(path):
    with open(path) as f
        return json.load(f)


def greeting(path):
    data = load(path)
    return f"Hallo {data['name']}, Version {data['version']}"
'''
_APP_JSON_BROKEN = '{\n  "name": "Orb",\n  "version": 3,\n}\n'
_APP_TEST = '''from app import greeting


def test_greeting():
    assert greeting("app.json") == "Hallo Orb, Version 3"
'''

# ---------------------------------------------------------------- 8 genaue Änderung in einer großen Datei
_HANDLERS = "\n\n".join(
    f"def handler_{i}(event):\n    \"\"\"Verarbeitet Ereignisse vom Typ {i}.\"\"\"\n"
    f"    if not event:\n        return None\n    value = event.get(\"value\", 0)\n    return value + {i}"
    for i in range(1, 41)) + "\n"
_HANDLERS_TEST = '''import handlers


def test_all_handlers_add_their_number_except_17():
    for i in range(1, 41):
        got = getattr(handlers, f"handler_{i}")({"value": 100})
        assert got == (100 * 17 if i == 17 else 100 + i), i


def test_empty_event():
    assert handlers.handler_17({}) is None and handlers.handler_18(None) is None
'''
_HANDLERS_SOLVED = _HANDLERS.replace("    return value + 17\n", "    return value * 17\n")

# ---------------------------------------------------------------- 9 Tests schreiben
_UTILS = '''def clamp(value, low, high):
    """Begrenzt value auf den Bereich [low, high]."""
    if value < low:
        return low
    if value > high:
        return high
    return value
'''
_UTILS_TEST_SOLVED = '''from utils import clamp


def test_inside():
    assert clamp(5, 0, 10) == 5


def test_below():
    assert clamp(-3, 0, 10) == 0


def test_above():
    assert clamp(99, 0, 10) == 10
'''


def _tests_written(project: Path, answer: str) -> tuple[bool, str]:  # noqa: ARG001
    test = project / "tests" / "test_utils.py"
    if not test.is_file():
        return False, "tests/test_utils.py fehlt"
    if len(re.findall(r"^def test_", test.read_text(encoding="utf-8"), re.M)) < 3:
        return False, "weniger als drei Testfälle"
    ok, why = pytest_ok(project)
    if not ok:
        return False, why
    # prüfen die Tests wirklich etwas? Mit einem kaputten clamp müssen sie rot werden
    utils = project / "utils.py"
    original = utils.read_text(encoding="utf-8")
    try:
        utils.write_text("def clamp(value, low, high):\n    return value\n", encoding="utf-8")
        broken_ok, _ = pytest_ok(project)
    finally:
        utils.write_text(original, encoding="utf-8")
    return not broken_ok, "Tests grün und erkennen ein kaputtes clamp" if not broken_ok else \
        "Tests bleiben auch mit kaputtem clamp grün"


# ---------------------------------------------------------------- 10 ausführen und berichten
_CALC = '''def add(a, b):
    return a + b


def sub(a, b):
    return a - b


def mul(a, b):
    return a * b
'''
_CALC_TEST = "from calc import add, mul, sub\n\n\n" + "\n\n".join(
    f"def test_{name}():\n    assert {expr}" for name, expr in (
        ("add", "add(2, 3) == 5"), ("add_negative", "add(-2, 3) == 1"), ("sub", "sub(5, 3) == 2"),
        ("sub_negative", "sub(3, 5) == -2"), ("mul", "mul(4, 3) == 12"), ("mul_zero", "mul(4, 0) == 0"),
        ("mul_wrong", "mul(2, 2) == 5"))) + "\n"


def _reported(project: Path, answer: str) -> tuple[bool, str]:  # noqa: ARG001
    numbers = set(re.findall(r"\b\d+\b", answer))
    ok = "7" in numbers and ("1" in numbers or "6" in numbers) and "mul_wrong" in answer
    return ok, "nennt 7 Tests, den Fehlschlag und test_mul_wrong" if ok else \
        "Antwort nennt nicht 7 Tests, 1 Fehlschlag (bzw. 6 bestanden) und test_mul_wrong"


TASKS: list[Task] = [
    Task("fix-bug", ("Fehler finden und beheben", "find and fix a bug"),
         ("Die Tests dieses Projekts schlagen fehl. Finde die Ursache und behebe sie.",
          "The tests of this project fail. Find the cause and fix it."),
         {"stats.py": _STATS, "tests/test_stats.py": _STATS_TEST}, _tests, protect=("tests/test_stats.py",),
         solution={"stats.py": _STATS.replace("        return ordered[mid]\n    return",
                                              "        return (ordered[mid - 1] + ordered[mid]) / 2\n    return")}),
    Task("add-function", ("Funktion nach Tests ergänzen", "add a function to match the tests"),
         ("In tests/test_text.py stehen Tests für eine Funktion slugify, die es in text.py noch nicht gibt. "
          "Schreibe sie so, dass alle Tests durchlaufen.",
          "tests/test_text.py has tests for a function slugify that does not exist in text.py yet. Write it so "
          "that all tests pass."),
         {"text.py": _TEXT, "tests/test_text.py": _TEXT_TEST}, _tests, protect=("tests/test_text.py",),
         solution={"text.py": _TEXT_SOLVED}),
    Task("rename", ("Umbenennen über mehrere Dateien", "rename across several files"),
         ("Benenne die Funktion calc in cart.py in compute_total um – überall, wo sie benutzt wird. Die Tests "
          "erwarten schon den neuen Namen.",
          "Rename the function calc in cart.py to compute_total – everywhere it is used. The tests already expect "
          "the new name."),
         {"cart.py": _CART, "order.py": _ORDER, "report.py": _REPORT, "tests/test_cart.py": _CART_TEST}, _renamed,
         protect=("tests/test_cart.py",),
         solution={"cart.py": _CART.replace("calc", "compute_total"), "order.py": _ORDER.replace("calc", "compute_total"),
                   "report.py": _REPORT.replace("calc", "compute_total")}),
    Task("change-value", ("Ein Wert an drei Stellen", "one value in three places"),
         ("Der Server soll künftig auf Port 8080 statt 8000 laufen. Ändere das überall im Projekt, auch in der "
          "Dokumentation.",
          "The server should run on port 8080 instead of 8000 from now on. Change it everywhere in the project, "
          "including the documentation."),
         {"config.py": _CONFIG, "settings.json": _SETTINGS, "README.md": _README}, _port,
         solution={"config.py": _CONFIG.replace("8000", "8080"), "settings.json": _SETTINGS.replace("8000", "8080"),
                   "README.md": _README.replace("8000", "8080")}),
    Task("find-code", ("Code finden, nur antworten", "find code, answer only"),
         ("Wo wird in diesem Projekt der Rabatt für Kunden berechnet? Nenne Datei und Funktion. Ändere nichts.",
          "Where does this project calculate the customer discount? Name the file and the function. Change nothing."),
         {"pricing.py": _PRICING, "checkout.py": _CHECKOUT, "invoice.py": _INVOICE}, _found,
         answer="Der Rabatt wird in pricing.py in der Funktion apply_discount berechnet.", read_only=True),
    Task("write-script", ("Neues Skript schreiben", "write a new script"),
         ("Schreibe ein Skript wordcount.py: `python wordcount.py <datei>` gibt die Anzahl der Wörter in der Datei aus "
          "– nur die Zahl. Probiere es mit sample.txt aus.",
          "Write a script wordcount.py: `python wordcount.py <file>` prints the number of words in the file – just the "
          "number. Try it with sample.txt."),
         {"sample.txt": _SAMPLE}, _wordcount,
         solution={"wordcount.py": "import sys\n\nprint(len(open(sys.argv[1], encoding='utf-8').read().split()))\n"}),
    Task("fix-syntax", ("Syntaxfehler in zwei Dateien", "syntax errors in two files"),
         ("Das Projekt startet nicht mehr: es gibt Syntaxfehler. Behebe sie, bis der Test durchläuft.",
          "The project no longer starts: there are syntax errors. Fix them until the test passes."),
         {"app.py": _APP_BROKEN, "app.json": _APP_JSON_BROKEN, "tests/test_app.py": _APP_TEST}, _tests,
         protect=("tests/test_app.py",),
         solution={"app.py": _APP_BROKEN.replace("as f\n", "as f:\n"),
                   "app.json": _APP_JSON_BROKEN.replace('3,\n', '3\n')}),
    Task("precise-edit", ("Genaue Änderung in einer großen Datei", "precise edit in a large file"),
         ("In handlers.py soll handler_17 den Wert mit 17 multiplizieren statt 17 zu addieren. Alle anderen "
          "Funktionen bleiben, wie sie sind.",
          "In handlers.py, handler_17 should multiply the value by 17 instead of adding 17. All other functions "
          "stay as they are."),
         {"handlers.py": _HANDLERS, "tests/test_handlers.py": _HANDLERS_TEST}, _tests,
         protect=("tests/test_handlers.py",), solution={"handlers.py": _HANDLERS_SOLVED}),
    Task("write-tests", ("Tests schreiben", "write tests"),
         ("Schreibe Tests für die Funktion clamp aus utils.py in tests/test_utils.py – mindestens drei Fälle, die "
          "ihr Verhalten wirklich prüfen. Sie müssen durchlaufen.",
          "Write tests for the function clamp from utils.py in tests/test_utils.py – at least three cases that "
          "really check its behaviour. They must pass."),
         {"utils.py": _UTILS, "tests/__init__.py": ""}, _tests_written, protect=("utils.py",),
         solution={"tests/test_utils.py": _UTILS_TEST_SOLVED}),
    Task("run-and-report", ("Ausführen und berichten", "run and report"),
         ("Führe die Tests dieses Projekts aus und berichte: Wie viele Tests gibt es, wie viele schlagen fehl, und "
          "welcher? Ändere nichts.",
          "Run the tests of this project and report: how many tests are there, how many fail, and which one? "
          "Change nothing."),
         {"calc.py": _CALC, "tests/test_calc.py": _CALC_TEST}, _reported,
         answer="Es gibt 7 Tests, 6 bestehen, 1 schlägt fehl: test_mul_wrong.", read_only=True),
]

BY_NAME = {t.name: t for t in TASKS}
# Damit pytest die Module im Projektordner findet (kein Paket, kein Installieren)
CONFTEST = "import sys\nfrom pathlib import Path\n\nsys.path.insert(0, str(Path(__file__).parent))\n"
