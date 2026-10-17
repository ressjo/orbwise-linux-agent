"""PDFs im Chat: lokal ausgelesen, Text geht mit der Frage ans Modell (gekürzt, wenn er zu lang ist), die Oberfläche
zeigt nur Titel und Seitenzahl."""

import base64
import json

import pytest
from fastapi.testclient import TestClient

from orbwise import attachments


def make_pdf(pages: list[str]) -> bytes:
    """Kleines, gültiges PDF mit einer Textzeile je Seite (Helvetica)."""
    objs = ["<< /Type /Catalog /Pages 2 0 R >>", ""]
    kids, n = [], 3
    font = 3 + 2 * len(pages)
    for text in pages:
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {n + 1} 0 R "
                    f"/Resources << /Font << /F1 {font} 0 R >> >> >>")
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream.decode('latin-1')}\nendstream")
        kids.append(f"{n} 0 R")
        n += 2
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>"
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def data_url(raw: bytes) -> str:
    return "data:application/pdf;base64," + base64.b64encode(raw).decode()


def test_pdf_is_read_locally_and_remembered(cfg):
    name = attachments.save(cfg, make_pdf(["Rechnung Nr. 4711", "Summe 99 Euro"]), "Rechnung.pdf")
    assert name.endswith(".pdf") and attachments.path_of(cfg, name)
    d = attachments.doc(cfg, name)
    assert d["title"] == "Rechnung.pdf" and d["pages"] == 2
    assert "Rechnung Nr. 4711" in d["text"] and "Summe 99 Euro" in d["text"] and "--- Seite 2 ---" in d["text"]
    assert attachments.doc_info(d) == {"name": name, "title": "Rechnung.pdf", "pages": 2, "chars": len(d["text"]),
                                       "truncated": False, "scanned": False}
    # gleiche Datei unter anderem Namen: Titel neu, Text bleibt
    assert attachments.save(cfg, make_pdf(["Rechnung Nr. 4711", "Summe 99 Euro"]), "Kopie.pdf") == name
    assert attachments.doc(cfg, name)["title"] == "Kopie.pdf"
    with pytest.raises(ValueError):
        attachments.save(cfg, b"%PDF-1.4 kaputt", "x.pdf")
    with pytest.raises(ValueError):
        attachments.save(cfg, b"MZ\x90\x00 keine pdf", "x.exe")


def test_long_documents_are_shortened_with_a_note():
    docs = [{"name": "a.pdf", "title": "A.pdf", "pages": 9, "text": "x" * 10_000},
            {"name": "b.pdf", "title": "B.pdf", "pages": 1, "text": "kurz"}]
    a, b = attachments.fit_docs(docs, 6000)
    assert len(a["text"]) == 3000 and a["truncated"] and a["chars"] == 10_000
    assert b["text"] == "kurz" and not b["truncated"]
    block = attachments.doc_block(a)
    assert block.startswith("[Angehängtes PDF „A.pdf“, 9 Seiten – gekürzt: die ersten 3.000 von 10.000 Zeichen")
    assert block.endswith("[Ende des PDFs]")
    scanned = attachments.doc_block({"title": "Scan.pdf", "pages": 3, "text": ""})
    assert "kein Text auslesbar" in scanned
    assert attachments.doc_info({"name": "s.pdf", "title": "Scan.pdf", "pages": 3, "text": ""})["scanned"]


def _wait_done(ws):
    ended = False
    while True:
        ev = json.loads(ws.receive_text())
        ended = ended or ev.get("type") == "assistant_end"
        if ended and ev.get("type") == "state" and ev.get("state") == "idle":
            return


def test_pdf_in_the_chat(cfg, monkeypatch):
    monkeypatch.setenv("ORBWISE_FAKE_LLM", "1")
    monkeypatch.setenv("ORBWISE_SKIP_WARMUP", "1")
    from orbwise.server import create_app
    app = create_app(cfg)
    with TestClient(app, base_url="http://localhost:8765") as client:
        r = client.post("/api/attachments", json={"data": data_url(make_pdf(["Vertrag mit Kuendigungsfrist 3 Monate zum Quartalsende fuer Wohnung 12"])),
                                                  "name": "/tmp/../Vertrag.pdf"})
        info = r.json()
        name = info["name"]
        assert info["doc"]["title"] == "Vertrag.pdf" and info["doc"]["pages"] == 1 and not info["doc"]["scanned"]
        f = client.get(f"/api/attachments/{name}")
        assert f.headers["content-type"] == "application/pdf" and f.content.startswith(b"%PDF")
        assert client.post("/api/attachments", json={"data": "data:x;base64,AAAA"}).status_code == 400

        with client.websocket_connect("ws://localhost:8765/ws", headers={"Origin": "http://localhost:8765"}) as ws:
            ws.send_text(json.dumps({"type": "user_message", "text": "Welche Frist?", "images": [name]}))
            user_ev = None
            while user_ev is None:
                ev = json.loads(ws.receive_text())
                if ev.get("type") == "user":
                    user_ev = ev
            assert user_ev["images"] == [] and user_ev["files"][0]["title"] == "Vertrag.pdf"
            _wait_done(ws)

            msgs = client.get("/api/history").json()["messages"]
            user = msgs[0]
            assert user["content"] == "Welche Frist?" and "images" not in user
            assert user["files"] == [{"name": name, "title": "Vertrag.pdf", "pages": 1, "chars": user["files"][0]["chars"],
                                      "truncated": False, "scanned": False}]
            agent = app.state.hub.agent
            prompt = agent.build_messages(display=False)
            sent = next(m["content"] for m in prompt if m["role"] == "user")
            assert "Welche Frist?" in sent and "[Angehängtes PDF „Vertrag.pdf“, 1 Seite]" in sent
            assert "Kuendigungsfrist 3 Monate" in sent and sent.rstrip().endswith("[Ende des PDFs]")
            # fremder Inhalt: Aktionen fragen nach; Text daraus geht nicht unbemerkt ins Internet
            assert agent._tainted
            assert agent._leak("web_search", {"query": "Kuendigungsfrist 3 Monate zum Quartalsende fuer Wohnung 12"})
            assert not agent._leak("web_search", {"query": "Kündigungsfrist Mietrecht"})

            # neu generieren: das PDF bleibt an der Frage
            client.post("/api/chats/active/rewind", json={"turn": 1})
            _wait_done(ws)
            assert client.get("/api/history").json()["messages"][0]["files"][0]["name"] == name

        chat_id = client.get("/api/history").json()["chat"]["id"]
        assert "📎 Vertrag.pdf (1 Seite)" in client.get(f"/api/chats/{chat_id}/export").text
