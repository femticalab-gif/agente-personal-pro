"""``SessionStore`` cifrado sobre SQLite para el Agent SDK.

El CLI escribe primero el transcript en ``CLAUDE_CONFIG_DIR`` (montado en tmpfs, es decir,
en RAM) y el SDK refleja cada lote aquí, cifrado con Fernet. Al reanudar desde el store, el
SDK materializa el transcript en un directorio temporal y lo borra al terminar. Resultado:
los transcripts nunca quedan en claro en disco.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from claude_agent_sdk import (
    SessionKey,
    SessionListSubkeysKey,
    SessionStoreEntry,
    SessionStoreListEntry,
    SessionSummaryEntry,
    fold_session_summary,
)

from marc_agent.db import Database
from marc_agent.util import iso, utcnow


class EncryptedSessionStore:
    def __init__(self, db: Database) -> None:
        self._db = db
        self._lock = asyncio.Lock()
        self._last_mtime = 0

    def _mtime(self) -> int:
        now = int(time.time() * 1000)
        if now <= self._last_mtime:
            now = self._last_mtime + 1
        self._last_mtime = now
        return now

    async def append(self, key: SessionKey, entries: list[SessionStoreEntry]) -> None:
        subpath = key.get("subpath") or ""
        async with self._lock:
            with self._db.transaction():
                for entry in entries:
                    uuid = entry.get("uuid")
                    self._db.execute(
                        "INSERT OR IGNORE INTO session_entries(project_key, session_id, subpath, uuid, "
                        "created_at, payload) VALUES(?,?,?,?,?,?)",
                        (
                            key["project_key"],
                            key["session_id"],
                            subpath,
                            uuid,
                            iso(utcnow()),
                            self._db.enc(dict(entry)),
                        ),
                    )
                mtime = self._mtime()
                if not subpath:
                    row = self._db.query_one(
                        "SELECT summary FROM session_meta WHERE project_key=? AND session_id=?",
                        (key["project_key"], key["session_id"]),
                    )
                    prev: SessionSummaryEntry | None = (
                        self._db.dec(row["summary"]) if row is not None and row["summary"] else None
                    )
                    folded = fold_session_summary(prev, key, entries)
                    folded["mtime"] = mtime
                    self._db.execute(
                        "INSERT INTO session_meta(project_key, session_id, mtime, summary) "
                        "VALUES(?,?,?,?) ON CONFLICT(project_key, session_id) DO UPDATE SET "
                        "mtime=excluded.mtime, summary=excluded.summary",
                        (key["project_key"], key["session_id"], mtime, self._db.enc(dict(folded))),
                    )

    async def load(self, key: SessionKey) -> list[SessionStoreEntry] | None:
        rows = self._db.query(
            "SELECT payload FROM session_entries WHERE project_key=? AND session_id=? AND subpath=? "
            "ORDER BY seq",
            (key["project_key"], key["session_id"], key.get("subpath") or ""),
        )
        if not rows:
            return None
        entries: list[SessionStoreEntry] = [self._db.dec(r["payload"]) for r in rows]
        return entries

    async def list_sessions(self, project_key: str) -> list[SessionStoreListEntry]:
        rows = self._db.query(
            "SELECT session_id, mtime FROM session_meta WHERE project_key=?", (project_key,)
        )
        return [{"session_id": r["session_id"], "mtime": int(r["mtime"])} for r in rows]

    async def list_session_summaries(self, project_key: str) -> list[SessionSummaryEntry]:
        rows = self._db.query(
            "SELECT summary FROM session_meta WHERE project_key=? AND summary IS NOT NULL",
            (project_key,),
        )
        return [self._db.dec(r["summary"]) for r in rows]

    async def delete(self, key: SessionKey) -> None:
        async with self._lock:
            with self._db.transaction():
                if key.get("subpath"):
                    self._db.execute(
                        "DELETE FROM session_entries WHERE project_key=? AND session_id=? AND subpath=?",
                        (key["project_key"], key["session_id"], key["subpath"]),
                    )
                    return
                self._db.execute(
                    "DELETE FROM session_entries WHERE project_key=? AND session_id=?",
                    (key["project_key"], key["session_id"]),
                )
                self._db.execute(
                    "DELETE FROM session_meta WHERE project_key=? AND session_id=?",
                    (key["project_key"], key["session_id"]),
                )

    async def list_subkeys(self, key: SessionListSubkeysKey) -> list[str]:
        rows = self._db.query(
            "SELECT DISTINCT subpath FROM session_entries WHERE project_key=? AND session_id=? "
            "AND subpath<>''",
            (key["project_key"], key["session_id"]),
        )
        return [str(r["subpath"]) for r in rows]

    def delete_session_everywhere(self, session_id: str) -> None:
        self._db.execute("DELETE FROM session_entries WHERE session_id=?", (session_id,))
        self._db.execute("DELETE FROM session_meta WHERE session_id=?", (session_id,))

    def stats(self) -> dict[str, Any]:
        row = self._db.query_one(
            "SELECT count(*) AS n, count(DISTINCT session_id) AS s FROM session_entries"
        )
        return {"entries": int(row["n"]) if row else 0, "sessions": int(row["s"]) if row else 0}
