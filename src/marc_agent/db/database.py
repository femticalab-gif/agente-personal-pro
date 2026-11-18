"""Acceso a SQLite con cifrado de campos.

SQLite se usa de forma síncrona: es local, las consultas son pequeñas y el agente es
mono-usuario. Un ``RLock`` serializa el acceso desde corrutinas y hilos.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from marc_agent.crypto import Cipher
from marc_agent.db.schema import ENCRYPTED_COLUMNS, MIGRATIONS, PERSONAL_TABLES
from marc_agent.util import iso, utcnow

Params = Sequence[Any] | dict[str, Any]


class Database:
    def __init__(self, path: Path | str, cipher: Cipher) -> None:
        self.path = str(path)
        self.cipher = cipher
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._tx_depth = 0
        self._apply_pragmas()
        self.migrate()

    def _apply_pragmas(self) -> None:
        with self._lock:
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            # Sobrescribe con ceros el contenido borrado (borrado irrecuperable).
            self._conn.execute("PRAGMA secure_delete=ON")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.execute("PRAGMA temp_store=MEMORY")

    def migrate(self) -> None:
        with self._lock:
            current = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
            for version, script in enumerate(MIGRATIONS, start=1):
                if version <= current:
                    continue
                self._conn.executescript(
                    f"BEGIN;\n{script}\nPRAGMA user_version={version};\nCOMMIT;"
                )

    @property
    def schema_version(self) -> int:
        return int(self.query_one("PRAGMA user_version")[0])  # type: ignore[index]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- consultas -----------------------------------------------------------------
    def execute(self, sql: str, params: Params = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def executemany(self, sql: str, rows: Sequence[Params]) -> None:
        with self._lock:
            self._conn.executemany(sql, rows)

    def query(self, sql: str, params: Params = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Params = ()) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self._conn.execute(sql, params).fetchone()
            return row

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            outer = self._tx_depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            self._tx_depth += 1
            try:
                yield
            except BaseException:
                self._tx_depth -= 1
                if outer:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._tx_depth -= 1
                if outer:
                    self._conn.execute("COMMIT")

    # -- cifrado -------------------------------------------------------------------
    def enc(self, obj: Any) -> bytes:
        return self.cipher.encrypt_json(obj)

    def dec(self, blob: bytes) -> Any:
        return self.cipher.decrypt_json(blob)

    # -- estado clave/valor --------------------------------------------------------
    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.query_one("SELECT value FROM kv_state WHERE key=?", (key,))
        return default if row is None else str(row["value"])

    def set_state(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO kv_state(key, value, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, iso(utcnow())),
        )

    def delete_state(self, key: str) -> None:
        self.execute("DELETE FROM kv_state WHERE key=?", (key,))

    def get_secure(self, key: str, default: Any = None) -> Any:
        row = self.query_one("SELECT payload FROM kv_secure WHERE key=?", (key,))
        return default if row is None else self.dec(row["payload"])

    def set_secure(self, key: str, value: Any) -> None:
        self.execute(
            "INSERT INTO kv_secure(key, payload, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET payload=excluded.payload, "
            "updated_at=excluded.updated_at",
            (key, self.enc(value), iso(utcnow())),
        )

    def mark_once(self, key: str) -> bool:
        """Registra una notificación enviada. Devuelve ``False`` si ya existía."""
        cur = self.execute(
            "INSERT OR IGNORE INTO notifications_sent(key, sent_at) VALUES(?,?)",
            (key, iso(utcnow())),
        )
        return cur.rowcount == 1

    # -- mantenimiento -------------------------------------------------------------
    def backup_to(self, dest: Path) -> None:
        """Copia consistente con la API de backup de SQLite."""
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            target = sqlite3.connect(str(dest))
            try:
                self._conn.backup(target)
            finally:
                target.close()

    def rotate_encryption(self) -> int:
        """Re-cifra todos los campos con la clave primaria actual. Devuelve nº de campos."""
        count = 0
        with self.transaction():
            for table, column in ENCRYPTED_COLUMNS:
                rows = self.query(
                    f"SELECT rowid AS rid, {column} AS blob FROM {table} WHERE {column} IS NOT NULL"
                )
                for row in rows:
                    self.execute(
                        f"UPDATE {table} SET {column}=? WHERE rowid=?",
                        (self.cipher.rotate(row["blob"]), row["rid"]),
                    )
                    count += 1
        return count

    def wipe_personal_data(self) -> None:
        """Borra todos los datos personales. ``secure_delete`` sobrescribe las páginas."""
        with self.transaction():
            for table in PERSONAL_TABLES:
                self.execute(f"DELETE FROM {table}")
            self.execute("DELETE FROM api_usage")
            self.execute(
                "DELETE FROM kv_state WHERE key NOT IN ('kill_switch','dry_run','last_heartbeat')"
            )
        with self._lock:
            self._conn.execute("VACUUM")
            if self.path != ":memory:":
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def integrity_ok(self) -> bool:
        row = self.query_one("PRAGMA integrity_check")
        return row is not None and str(row[0]) == "ok"
