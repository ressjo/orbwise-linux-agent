"""Systembefehle werden nicht vorgelesen – nur die Frage, ob der Befehl ausgeführt werden soll."""

import asyncio

from orbwise.hub import Hub, spoken_confirm
from orbwise.voice.tts import SentenceSplitter, clean_for_speech


def test_commands_in_answers_are_not_spoken():
    assert "pacman" not in clean_for_speech("Ich führe `sudo pacman -Syu` aus.")
    assert clean_for_speech("Der Befehl: `sudo systemctl restart nginx`.") == "Der Befehl."
    assert clean_for_speech("Starte `firefox` jetzt.") == "Starte firefox jetzt."  # einzelne Wörter bleiben
    assert clean_for_speech("Datei `config.yaml` angepasst.") == "Datei config.yaml angepasst."
    assert clean_for_speech("```\nls -la\n```") == ""


def test_splitter_does_not_cut_inside_inline_code():
    sp, out = SentenceSplitter(), []
    for ch in "Ich führe gleich `cd /tmp; ls -la` für dich aus. Danach schauen wir weiter, ob alles klappt.":
        out += sp.feed(ch)
    out += sp.flush()
    spoken = [clean_for_speech(s) for s in out]
    assert spoken == ["Ich führe gleich für dich aus.", "Danach schauen wir weiter, ob alles klappt."]


def test_confirmation_asks_without_reading_the_command(cfg):
    assert spoken_confirm("run_shell", {"command": "sudo pacman -S firefox"}, cfg) == \
        "Möchtest du folgenden Befehl ausführen?"
    assert spoken_confirm("write_file", {"path": "/home/joshua/.config/x/settings.conf"}, cfg) == \
        "Soll ich das Schreiben der Datei settings.conf ausführen?"
    assert spoken_confirm("system_update", {}, cfg) == "Soll ich ein vollständiges Systemupdate ausführen?"
    cfg.language = "en"
    assert spoken_confirm("run_shell", {"command": "rm -rf build"}, cfg) == "Do you want to run the following command?"


def test_hub_speaks_question_but_dialog_shows_command(cfg):
    said, sent = [], []
    hub = Hub.__new__(Hub)
    hub.cfg, hub.pending, hub.editable_pending = cfg, {}, set()

    class Speaker:
        def say(self, text, *a):
            said.append(text)

    async def broadcast(ev):
        sent.append(ev)

    hub.speaker, hub.broadcast = Speaker(), broadcast

    async def scenario():
        task = asyncio.create_task(hub.confirm("c1", "run_shell", {"command": "sudo pacman -S firefox"}, ""))
        await asyncio.sleep(0.01)
        hub.resolve("c1", True)
        return await task

    assert asyncio.run(scenario()) is True
    assert said == ["Möchtest du folgenden Befehl ausführen?"]
    request = next(e for e in sent if e["type"] == "confirm_request")
    assert "sudo pacman -S firefox" in request["summary"] + str(request["args"])
