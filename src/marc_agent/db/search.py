"""Índice de búsqueda FTS5 **en memoria**.

Un índice FTS5 en disco guardaría los términos en claro. Por eso el índice vive solo en
RAM: se reconstruye al arrancar desde los datos descifrados y se mantiene sincronizado en
cada escritura. Para un agente personal el volumen es pequeño.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass

from marc_agent.util import strip_accents

_WORD = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True)
class Hit:
    kind: str
    id: str
    title: str
    snippet: str
    score: float


class SearchIndex:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self._conn.execute(
            "CREATE VIRTUAL TABLE idx USING fts5("
            "kind UNINDEXED, id UNINDEXED, title, body, "
            "tokenize='unicode61 remove_diacritics 2')"
        )

    def upsert(self, kind: str, item_id: str, title: str, body: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM idx WHERE kind=? AND id=?", (kind, item_id))
            self._conn.execute(
                "INSERT INTO idx(kind, id, title, body) VALUES(?,?,?,?)",
                (kind, item_id, title, body),
            )

    def delete(self, kind: str, item_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM idx WHERE kind=? AND id=?", (kind, item_id))

    def clear(self, kind: str | None = None) -> None:
        with self._lock:
            if kind is None:
                self._conn.execute("DELETE FROM idx")
            else:
                self._conn.execute("DELETE FROM idx WHERE kind=?", (kind,))

    def count(self, kind: str | None = None) -> int:
        with self._lock:
            if kind is None:
                row = self._conn.execute("SELECT count(*) FROM idx").fetchone()
            else:
                row = self._conn.execute(
                    "SELECT count(*) FROM idx WHERE kind=?", (kind,)
                ).fetchone()
            return int(row[0])

    @staticmethod
    def _terms(query: str) -> list[str]:
        return [strip_accents(t).lower() for t in _WORD.findall(query) if len(t) > 1][:12]

    def search(self, query: str, kinds: list[str] | None = None, limit: int = 10) -> list[Hit]:
        terms = self._terms(query)
        if not terms:
            return []
        for joiner in (" AND ", " OR "):
            expr = joiner.join(f'"{t}"*' for t in terms)
            hits = self._run(expr, kinds, limit)
            if hits:
                return hits
        return []

    def _run(self, expr: str, kinds: list[str] | None, limit: int) -> list[Hit]:
        sql = (
            "SELECT kind, id, title, snippet(idx, 3, '[', ']', '…', 12) AS snip, bm25(idx) AS score "
            "FROM idx WHERE idx MATCH ?"
        )
        params: list[object] = [expr]
        if kinds:
            sql += " AND kind IN (" + ",".join("?" for _ in kinds) + ")"
            params.extend(kinds)
        sql += " ORDER BY score LIMIT ?"
        params.append(limit)
        with self._lock:
            try:
                rows = self._conn.execute(sql, params).fetchall()
            except sqlite3.OperationalError:
                return []
        return [Hit(r[0], r[1], r[2], r[3], float(r[4])) for r in rows]
