"""Suchindex über das Gedächtnis: SQLite (FTS5 Volltext) + Embeddings, kombiniert per Reciprocal Rank Fusion.

Der Index ist nur ein Cache – die Wahrheit liegt in den Markdown-Dateien und kann mit
`orbwise reindex` jederzeit neu aufgebaut werden.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import math
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks(
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    day TEXT NOT NULL,
    source TEXT NOT NULL,
    text TEXT NOT NULL,
    hash TEXT NOT NULL UNIQUE,
    embedding BLOB,
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_source ON chunks(source);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, content='chunks', content_rowid='id', tokenize='unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, text) VALUES ('delete', old.id, old.text);
END;
"""

MAX_CHUNK_CHARS = 1500
CORRUPT_WORDS = ("malformed", "corrupt", "not a database", "vtable constructor failed")
RRF_K = 60


@dataclass
class Hit:
    id: int
    kind: str
    day: str
    text: str
    created: float
    score: float
    sim: float | None = None  # Embedding-Ähnlichkeit zur Anfrage (None ohne Embeddings)


def split_text(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    text = text.strip()
    if len(text) <= max_chars:
        return [text] if text else []
    parts, buf = [], ""
    for para in re.split(r"\n\s*\n", text):
        while len(para) > max_chars:
            if buf:
                parts.append(buf)
                buf = ""
            parts.append(para[:max_chars])
            para = para[max_chars:]
        if len(buf) + len(para) + 2 > max_chars and buf:
            parts.append(buf)
            buf = ""
        buf = f"{buf}\n\n{para}" if buf else para
    if buf:
        parts.append(buf)
    return parts


def is_corrupt(e: Exception) -> bool:
    return isinstance(e, sqlite3.DatabaseError) and any(w in str(e).lower() for w in CORRUPT_WORDS)


def check_file(path: Path) -> bool:
    """Prüft eine Index-Datei, ohne sie zu verändern (für `orbwise doctor`)."""
    if not path.exists():
        return True
    try:
        db = sqlite3.connect(path, timeout=5)
        try:
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                return False
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='chunks_fts'").fetchone():
                db.execute("INSERT INTO chunks_fts(chunks_fts, rank) VALUES('integrity-check', 1)")
                db.rollback()  # die Prüfung schreibt nichts – offene Transaktion nicht halten
            return True
        finally:
            db.close()
    except sqlite3.OperationalError as e:
        # gerade von Orbwise in Benutzung → nicht prüfbar, aber deshalb nicht kaputt
        return "locked" in str(e) or "busy" in str(e)
    except sqlite3.DatabaseError:
        return False


def shared_words(query: str, text: str) -> int:
    """Wie viele Wörter (ab 3 Zeichen) der Anfrage im Text vorkommen."""
    have = set(re.findall(r"\w{3,}", text.lower()))
    return sum(1 for w in dict.fromkeys(re.findall(r"\w{3,}", query.lower())) if w in have)


def fts_query(text: str) -> str:
    words = [w for w in re.findall(r"\w{3,}", text.lower())][:24]
    return " OR ".join(f'"{w}"' for w in dict.fromkeys(words))


class MemoryIndex:
    def __init__(self, path: Path, embedder=None, min_similarity: float = 0.3):
        self.path = path
        self.embedder = embedder  # Objekt mit async embed(list[str]) -> list[list[float]]
        self.min_similarity = min_similarity
        self._matrix: np.ndarray | None = None
        self._matrix_ids: list[int] = []
        self._matrix_kinds: list[str] = []
        self._lock = asyncio.Lock()
        self._embed_warned = False
        # True, wenn die Datei beschädigt war und leer neu angelegt wurde → Memory.rebuild_index() füllt sie wieder
        self.needs_rebuild = False
        try:
            self.db = self._open()
            self._check()
        except sqlite3.DatabaseError as e:
            if not is_corrupt(e):
                raise
            self.heal(e)

    def close(self) -> None:
        self.db.close()

    # ---------- Selbstheilung ----------
    # Der Index ist nur ein Cache der Markdown-Dateien: Ist er beschädigt („database disk image is malformed“),
    # wird erst der Volltextindex neu aufgebaut, notfalls die Datei beiseitegelegt und neu angelegt.
    def _open(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, check_same_thread=False)
        db.execute("PRAGMA busy_timeout=5000")  # zweiter Prozess (z. B. `orbwise reindex`) wartet statt zu scheitern
        with contextlib.suppress(sqlite3.DatabaseError):
            db.execute("PRAGMA journal_mode=WAL")
        db.executescript(SCHEMA)
        return db

    def _check(self) -> None:
        """Wirft sqlite3.DatabaseError, wenn Datei oder Volltextindex beschädigt sind."""
        result = self.db.execute("PRAGMA quick_check").fetchone()[0]
        if result != "ok":
            raise sqlite3.DatabaseError(f"database disk image is malformed ({result})")
        busy = self.db.in_transaction
        self.db.execute("INSERT INTO chunks_fts(chunks_fts, rank) VALUES('integrity-check', 1)")
        if not busy:
            self.db.rollback()  # sonst hielte die (schreibfreie) Prüfung die Schreibsperre bis zum nächsten commit

    def heal(self, error: Exception | str) -> None:
        log.warning("Gedächtnis-Suchindex beschädigt (%s) – wird repariert", error)
        self._matrix = None
        with contextlib.suppress(Exception):
            self.db.rollback()
        try:  # Stufe 1: nur der Volltextindex passt nicht zur Tabelle
            self.db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")
            self.db.commit()
            self._check()
            log.info("Volltextindex neu aufgebaut")
            return
        except Exception:  # noqa: BLE001 – dann ist die Datei selbst kaputt
            pass
        self._replace_file()

    def _replace_file(self) -> None:
        """Stufe 2: beschädigte Datei beiseitelegen und leer neu anlegen (Inhalt kommt aus den Markdown-Dateien)."""
        with contextlib.suppress(Exception):
            self.db.close()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for suffix in ("", "-wal", "-shm", "-journal"):
            f = Path(f"{self.path}{suffix}")
            if f.exists():
                f.replace(f"{self.path}.corrupt-{stamp}{suffix}")
        self.db = self._open()
        self._matrix = None
        self.needs_rebuild = True
        log.warning("Beschädigter Suchindex nach %s.corrupt-%s verschoben – wird neu aufgebaut", self.path, stamp)

    def _guard(self, fn):
        """Datenbank-Zugriff; bei Beschädigung reparieren und einmal wiederholen."""
        try:
            return fn()
        except sqlite3.DatabaseError as e:
            if not is_corrupt(e):
                raise
            self.heal(e)
            return fn()

    def reset(self) -> None:
        """Index komplett neu anlegen (für den Neuaufbau – funktioniert auch bei beschädigter Datei)."""
        with contextlib.suppress(Exception):
            self.db.close()
        for suffix in ("", "-wal", "-shm", "-journal"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)
        self.db = self._open()
        self._matrix = None

    async def _embed(self, texts: list[str]) -> list[np.ndarray | None]:
        if not self.embedder or not texts:
            return [None] * len(texts)
        try:
            vecs = await self.embedder.embed(texts)
            return [np.asarray(v, dtype=np.float32) for v in vecs]
        except Exception as e:  # noqa: BLE001 – ohne Embeddings bleibt die Volltextsuche
            if not self._embed_warned:
                log.warning("Embeddings nicht verfügbar, nutze nur Volltextsuche: %s", e)
                self._embed_warned = True
            return [None] * len(texts)

    async def add(self, kind: str, day: str, source: str, text: str, created: float | None = None) -> int:
        chunks = split_text(text)
        if not chunks:
            return 0
        created = created or time.time()
        hashed = [(c, hashlib.sha1(f"{source}\n{c}".encode()).hexdigest()) for c in chunks]
        new = self._guard(lambda: [(c, h) for c, h in hashed
                                   if not self.db.execute("SELECT 1 FROM chunks WHERE hash=?", (h,)).fetchone()])
        if not new:
            return 0
        vecs = await self._embed([c for c, _ in new])

        def insert() -> None:
            for (c, h), v in zip(new, vecs):
                blob = v.tobytes() if v is not None else None
                self.db.execute(
                    "INSERT OR IGNORE INTO chunks(kind, day, source, text, hash, embedding, created) VALUES (?,?,?,?,?,?,?)",
                    (kind, day, source, c, h, blob, created),
                )
            self.db.commit()

        async with self._lock:
            self._guard(insert)
            self._matrix = None
        return len(new)

    def retag(self, source: str, kind: str) -> int:
        """Alle Einträge einer Quelle einem anderen Gedächtnis zuordnen (z. B. alte Coding-Chats → ihr Projekt)."""
        def run() -> int:
            n = self.db.execute("UPDATE chunks SET kind=? WHERE source=? AND kind!=?", (kind, source, kind)).rowcount
            self.db.commit()
            return n
        n = self._guard(run)
        if n:
            self._matrix = None
        return n

    def delete_day(self, kinds: tuple[str, ...], day: str) -> int:
        """Einträge eines Tages aus bestimmten Gedächtnis-Arten löschen (Tagebuch eines Tages vergessen)."""
        if not kinds:
            return 0

        def run() -> int:
            marks = ",".join("?" * len(kinds))
            n = self.db.execute(f"DELETE FROM chunks WHERE day=? AND kind IN ({marks})", (day, *kinds)).rowcount  # noqa: S608
            self.db.commit()
            return n
        n = self._guard(run)
        self._matrix = None
        return n or 0

    def delete_source(self, source: str) -> None:
        def run() -> None:
            self.db.execute("DELETE FROM chunks WHERE source=?", (source,))
            self.db.commit()
        self._guard(run)
        self._matrix = None

    def clear(self) -> None:
        def run() -> None:
            self.db.execute("DELETE FROM chunks")
            self.db.commit()
        self._guard(run)
        self._matrix = None

    def count(self) -> int:
        return self._guard(lambda: self.db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0])

    def healthy(self) -> bool:
        try:
            self._check()
            return True
        except sqlite3.DatabaseError:
            return False

    def _load_matrix(self) -> None:
        rows = self._guard(lambda: self.db.execute(
            "SELECT id, embedding, kind FROM chunks WHERE embedding IS NOT NULL").fetchall())
        vecs, ids, kinds = [], [], []
        dims: dict[int, int] = {}
        for _, blob, _ in rows:
            dims[len(blob)] = dims.get(len(blob), 0) + 1
        if not dims:
            self._matrix, self._matrix_ids, self._matrix_kinds = np.zeros((0, 1), dtype=np.float32), [], []
            return
        size = max(dims, key=dims.get)  # bei gewechseltem Embedding-Modell die häufigste Dimension
        for rid, blob, kind in rows:
            if len(blob) == size:
                v = np.frombuffer(blob, dtype=np.float32)
                n = np.linalg.norm(v)
                vecs.append(v / n if n else v)
                ids.append(rid)
                kinds.append(kind)
        self._matrix = np.vstack(vecs)
        self._matrix_ids = ids
        self._matrix_kinds = kinds

    async def search(self, query: str, k: int = 6, exclude_after: float | None = None,
                     kinds: tuple[str, ...] | None = None, exclude_source: str | None = None) -> list[Hit]:
        """Hybride Suche. exclude_after: Journal-Einträge ab diesem Zeitpunkt ignorieren
        (die stehen ohnehin noch wörtlich im aktuellen Gesprächsverlauf). Mit exclude_source gilt das nur für
        Einträge dieser Quelle (des aktuellen Chats) – andere Chats bleiben auffindbar."""
        ranks: dict[int, float] = {}
        # Nur im gewünschten Gedächtnis (Tools bzw. ein Projekt) suchen – schon vor dem Ranking, sonst verdrängen
        # Treffer aus dem anderen die eigenen
        only = f" AND c.kind IN ({','.join('?' * len(kinds))})" if kinds else ""

        q = fts_query(query)
        if q:
            try:
                rows = self._guard(lambda: self.db.execute(
                    "SELECT chunks_fts.rowid FROM chunks_fts JOIN chunks c ON c.id = chunks_fts.rowid "
                    f"WHERE chunks_fts MATCH ?{only} ORDER BY bm25(chunks_fts) LIMIT 30", (q, *(kinds or ()))
                ).fetchall())
                for rank, (rid,) in enumerate(rows):
                    ranks[rid] = ranks.get(rid, 0) + 1 / (RRF_K + rank)
            except sqlite3.OperationalError as e:
                log.debug("FTS-Fehler: %s", e)

        (qvec,) = await self._embed([query])
        sim_of: dict[int, float] = {}
        if qvec is not None:
            if self._matrix is None:
                self._load_matrix()
            if self._matrix is not None and len(self._matrix_ids) and self._matrix.shape[1] == qvec.shape[0]:
                qn = qvec / (np.linalg.norm(qvec) or 1)
                sims = self._matrix @ qn
                if kinds:
                    sims = np.where(np.isin(np.array(self._matrix_kinds, dtype=object), list(kinds)), sims, -2.0)
                order = np.argsort(-sims)[:30]
                for rank, idx in enumerate(order):
                    if sims[idx] < self.min_similarity:
                        break
                    rid = self._matrix_ids[idx]
                    ranks[rid] = ranks.get(rid, 0) + 1 / (RRF_K + rank)
                # Ähnlichkeit auch für reine Volltext-Treffer – damit lässt sich die Relevanz prüfen
                wanted = set(ranks)
                for idx, rid in enumerate(self._matrix_ids):
                    if rid in wanted:
                        sim_of[rid] = float(sims[idx])

        if not ranks:
            return []
        placeholders = ",".join("?" * len(ranks))
        rows = self._guard(lambda: self.db.execute(
            f"SELECT id, kind, day, text, created, source FROM chunks WHERE id IN ({placeholders})", list(ranks)
        ).fetchall())
        now = time.time()
        hits = []
        for rid, kind, day, text, created, source in rows:
            if kinds and kind not in kinds:
                continue
            if exclude_after is not None and kind.split(":")[0] in ("journal", "code") and created >= exclude_after and \
                    (exclude_source is None or source == exclude_source):
                continue
            age_days = max(0.0, (now - created) / 86400)
            recency = 0.004 * math.exp(-age_days / 30)  # leichte Bevorzugung frischer Erinnerungen
            bonus = 0.003 if kind.split(":")[0] in ("fact", "summary", "codefact") else 0.0
            hits.append(Hit(rid, kind, day, text, created, ranks[rid] + recency + bonus, sim_of.get(rid)))
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:k]
