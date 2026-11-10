"""Repo-Map: Symbole je Datei (Python über ast, andere Sprachen über Muster, ctags falls vorhanden), Cache, Werkzeug map."""

import os
import shutil

from conftest import run

from orbwise import repomap
from orbwise.tools.registry import ToolContext, get_tool, load_all_tools


def _proj(tmp_path):
    p = tmp_path / "proj"
    (p / "src" / "billing").mkdir(parents=True)
    (p / "web").mkdir()
    (p / "node_modules" / "x").mkdir(parents=True)
    (p / "src" / "billing" / "invoice.py").write_text(
        "class Invoice:\n    def total(self, tax=0.19):\n        return 1\n\n    async def pdf(self, *args, **kw):\n"
        "        pass\n\n\ndef make_invoice(customer):\n    return Invoice()\n", encoding="utf-8")
    (p / "web" / "app.ts").write_text(
        "export class Cart {\n  addItem(id: string, n: number): void {\n    return;\n  }\n}\n"
        "export function checkout(cart: Cart) {\n}\nexport const total = (items: Item[]) => 0;\n"
        "export interface Item { id: string }\nexport type Price = number;\n", encoding="utf-8")
    (p / "srv.go").write_text("package main\n\ntype Server struct{}\n\nfunc (s *Server) Start(port int) error {\n"
                              "\treturn nil\n}\n\nfunc main() {}\n", encoding="utf-8")
    (p / "lib.rs").write_text("pub struct Pool;\nimpl Pool {\n    pub fn get(&self, id: u32) -> u32 { id }\n}\n"
                              "pub trait Store {}\n", encoding="utf-8")
    (p / "node_modules" / "x" / "index.js").write_text("function hidden() {}\n")
    return p


def test_symbols_per_language(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    idx = repomap.index(_proj(tmp_path))
    assert "node_modules/x/index.js" not in idx
    py = {(s["kind"], s["name"], s["sig"]) for s in idx["src/billing/invoice.py"]}
    assert ("class", "Invoice", "") in py and ("method", "total", "(self, tax)") in py
    assert ("method", "pdf", "(self, *args, **kw)") in py and ("def", "make_invoice", "(customer)") in py
    ts = {(s["kind"], s["name"]) for s in idx["web/app.ts"]}
    assert {("class", "Cart"), ("method", "addItem"), ("function", "checkout"), ("const", "total"),
            ("interface", "Item"), ("type", "Price")} <= ts
    go = {(s["kind"], s["name"]) for s in idx["srv.go"]}
    assert {("type", "Server"), ("func", "Start"), ("func", "main")} <= go
    rs = {(s["kind"], s["name"]) for s in idx["lib.rs"]}
    assert {("struct", "Pool"), ("fn", "get"), ("trait", "Store")} <= rs


def test_render_overview_query_and_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    p = _proj(tmp_path)
    idx = repomap.index(p)
    out = repomap.render(idx)
    assert "src/billing/invoice.py: L1 class Invoice" in out and "L9 def make_invoice(customer)" in out
    assert repomap.render(idx, "web").startswith("web/app.ts:")
    hit = repomap.render(idx, query="total")
    assert "src/billing/invoice.py: L2 def Invoice.total(self, tax)" in hit and "web/app.ts" in hit
    assert "Kein Symbol" in repomap.render(idx, query="gibtsnicht")
    # Cache: geänderte Datei wird neu gelesen, die anderen nicht
    f = p / "srv.go"
    f.write_text("package main\n\nfunc Stop() {}\n", encoding="utf-8")
    os.utime(f, ns=(f.stat().st_mtime_ns + 10**9, f.stat().st_mtime_ns + 10**9))
    calls = []
    real = repomap.symbols
    monkeypatch.setattr(repomap, "symbols", lambda path, text: calls.append(path.name) or real(path, text))
    idx2 = repomap.index(p)
    assert calls == ["srv.go"] and [s["name"] for s in idx2["srv.go"]] == ["Stop"]
    # kurzes Limit: Hinweis zum Eingrenzen
    assert "eingrenzen" in repomap.render(idx2, limit=120)


def test_ctags_is_used_when_available(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    p = tmp_path / "big"
    p.mkdir()
    for i in range(25):
        (p / f"m{i}.c").write_text(f"int fn{i}(int a) {{\n  return a;\n}}\n")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "ctags").write_text(
        "#!/bin/sh\nif [ \"$1\" = --version ]; then echo 'Universal Ctags 6.1'; exit 0; fi\n"
        "while read f; do n=$(basename $f .c); echo \"{\\\"_type\\\":\\\"tag\\\",\\\"name\\\":\\\"from_ctags_$n\\\","
        "\\\"path\\\":\\\"$f\\\",\\\"line\\\":1,\\\"kind\\\":\\\"function\\\",\\\"signature\\\":\\\"(int a)\\\"}\"; done\n")
    (fake / "ctags").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{os.environ['PATH']}")
    idx = repomap.index(p)
    assert idx["m3.c"][0]["name"] == "from_ctags_m3" and idx["m3.c"][0]["sig"] == "(int a)"


def test_map_tool(tmp_path, cfg, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    p = _proj(tmp_path)
    load_all_tools()
    ctx = ToolContext(cfg=cfg, memory=None, cwd=str(p))
    out = run(get_tool("map").func(ctx, query="Invoice"))
    assert out.startswith("src/billing/invoice.py: L1 class Invoice")
    assert run(get_tool("map").func(ctx, path="src")).startswith("src/billing/invoice.py:")
    assert "nicht im Projektordner" in run(get_tool("map").func(ctx, path="/etc"))
    assert "Kein Projektordner" in run(get_tool("map").func(ToolContext(cfg=cfg, memory=None, cwd=""), query="x"))


def test_git_ls_files_respects_gitignore(tmp_path, monkeypatch):
    if not shutil.which("git"):
        return
    import subprocess
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    p = _proj(tmp_path)
    (p / "gen").mkdir()
    (p / "gen" / "out.py").write_text("def generated(): pass\n")
    (p / ".gitignore").write_text("gen/\n")
    subprocess.run(["git", "init", "-q"], cwd=p, check=True)
    files = repomap.files(p)
    assert "src/billing/invoice.py" in files and not any(f.startswith("gen/") for f in files)
