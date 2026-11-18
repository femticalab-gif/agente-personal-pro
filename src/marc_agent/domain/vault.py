"""Vault local cifrado para documentos que Marc envía por Telegram.

Cada fichero se guarda cifrado (Fernet) en ``/data/vault/<id>.bin``; los metadatos y el
texto extraído van cifrados en SQLite. Búsqueda FTS en el índice en memoria.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from marc_agent.crypto import Cipher, sha256_hex
from marc_agent.db import Database, SearchIndex
from marc_agent.tools.base import ToolError
from marc_agent.util import iso, new_id, parse_date, utcnow

DOC_TYPES = (
    "tique",
    "factura",
    "receta",
    "informe_medico",
    "analitica",
    "contrato",
    "identidad",
    "seguro",
    "garantia",
    "nomina",
    "fiscal",
    "otro",
)
MAX_FILE = 25 * 1024 * 1024


class VaultService:
    KIND = "doc"

    def __init__(self, db: Database, index: SearchIndex, cipher: Cipher, vault_dir: Path) -> None:
        self._db = db
        self._index = index
        self._cipher = cipher
        self._dir = vault_dir
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, doc_id: str) -> Path:
        if not doc_id.startswith("doc-") or not doc_id[4:].isalnum():
            raise ToolError("Id de documento inválido")
        return self._dir / f"{doc_id}.bin"

    def store(
        self,
        data: bytes,
        mime: str,
        filename: str,
        *,
        title: str,
        doc_type: str = "otro",
        doc_date: str | None = None,
        expires_at: str | None = None,
        text: str = "",
        extracted: dict[str, Any] | None = None,
        status: str = "archivado",
    ) -> dict[str, Any]:
        if len(data) > MAX_FILE:
            raise ToolError("Documento demasiado grande para el vault (>25 MB)")
        doc_id = new_id("doc")
        path = self._path(doc_id)
        path.write_bytes(self._cipher.encrypt_bytes(data))
        payload = {
            "titulo": title,
            "tipo": doc_type if doc_type in DOC_TYPES else "otro",
            "mime": mime,
            "fichero": filename,
            "texto": text[:100_000],
            "extraido": extracted or {},
        }
        now = iso(utcnow())
        self._db.execute(
            "INSERT INTO vault_docs(id, created_at, updated_at, doc_date, expires_at, status, size, "
            "sha256, payload) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                doc_id,
                now,
                now,
                parse_date(doc_date).isoformat() if doc_date else None,
                parse_date(expires_at).isoformat() if expires_at else None,
                status,
                len(data),
                sha256_hex(data),
                self._db.enc(payload),
            ),
        )
        self._index.upsert(self.KIND, doc_id, title, f"{payload['tipo']} {text}")
        return self.get(doc_id)

    def _public(self, row: Any, with_text: bool = False, text_limit: int = 4000) -> dict[str, Any]:
        payload = self._db.dec(row["payload"])
        out = {
            "id": row["id"],
            "titulo": payload["titulo"],
            "tipo": payload["tipo"],
            "fecha_doc": row["doc_date"],
            "caduca": row["expires_at"],
            "estado": row["status"],
            "fichero": payload.get("fichero", ""),
            "tamano": row["size"],
            "datos_extraidos": payload.get("extraido", {}),
        }
        if with_text:
            out["texto"] = str(payload.get("texto", ""))[:text_limit]
        return out

    def get(self, doc_id: str, with_text: bool = False) -> dict[str, Any]:
        row = self._db.query_one("SELECT * FROM vault_docs WHERE id=?", (doc_id,))
        if row is None:
            raise ToolError(f"No existe el documento {doc_id}")
        return self._public(row, with_text)

    def read_file(self, doc_id: str) -> tuple[bytes, str, str]:
        row = self._db.query_one("SELECT * FROM vault_docs WHERE id=?", (doc_id,))
        if row is None:
            raise ToolError(f"No existe el documento {doc_id}")
        payload = self._db.dec(row["payload"])
        data = self._cipher.decrypt_bytes(self._path(doc_id).read_bytes())
        if sha256_hex(data) != row["sha256"]:
            raise ToolError("El documento del vault no supera la verificación de integridad")
        return data, payload.get("mime", "application/octet-stream"), payload.get("fichero", doc_id)

    def update(
        self,
        doc_id: str,
        *,
        title: str | None = None,
        doc_type: str | None = None,
        doc_date: str | None = None,
        expires_at: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        row = self._db.query_one("SELECT * FROM vault_docs WHERE id=?", (doc_id,))
        if row is None:
            raise ToolError(f"No existe el documento {doc_id}")
        payload = self._db.dec(row["payload"])
        if title:
            payload["titulo"] = title
        if doc_type:
            payload["tipo"] = doc_type if doc_type in DOC_TYPES else "otro"
        self._db.execute(
            "UPDATE vault_docs SET payload=?, doc_date=?, expires_at=?, status=?, updated_at=? "
            "WHERE id=?",
            (
                self._db.enc(payload),
                parse_date(doc_date).isoformat() if doc_date else row["doc_date"],
                parse_date(expires_at).isoformat() if expires_at else row["expires_at"],
                status or row["status"],
                iso(utcnow()),
                doc_id,
            ),
        )
        self._index.upsert(
            self.KIND, doc_id, payload["titulo"], f"{payload['tipo']} {payload.get('texto', '')}"
        )
        return self.get(doc_id)

    def delete(self, doc_id: str) -> bool:
        path = self._path(doc_id)
        _shred(path)
        self._index.delete(self.KIND, doc_id)
        return self._db.execute("DELETE FROM vault_docs WHERE id=?", (doc_id,)).rowcount == 1

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        out = []
        for hit in self._index.search(query, kinds=[self.KIND], limit=limit):
            row = self._db.query_one("SELECT * FROM vault_docs WHERE id=?", (hit.id,))
            if row is not None:
                doc = self._public(row)
                doc["fragmento"] = hit.snippet
                out.append(doc)
        return out

    def list_docs(self, doc_type: str | None = None, limit: int = 30) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM vault_docs ORDER BY created_at DESC LIMIT ?", (limit * 3,)
        )
        docs = [self._public(r) for r in rows]
        if doc_type:
            docs = [d for d in docs if d["tipo"] == doc_type]
        return docs[:limit]

    def expiring(self, today: date, within_days: int) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM vault_docs WHERE expires_at IS NOT NULL AND expires_at>=? AND expires_at<=? "
            "ORDER BY expires_at",
            (today.isoformat(), (today + timedelta(days=within_days)).isoformat()),
        )
        return [self._public(r) for r in rows]

    def rebuild_index(self) -> None:
        self._index.clear(self.KIND)
        for row in self._db.query("SELECT * FROM vault_docs"):
            payload = self._db.dec(row["payload"])
            self._index.upsert(
                self.KIND,
                row["id"],
                payload["titulo"],
                f"{payload['tipo']} {payload.get('texto', '')}",
            )

    def wipe_files(self) -> int:
        count = 0
        for path in self._dir.glob("*.bin"):
            _shred(path)
            count += 1
        return count


def _shred(path: Path) -> None:
    """Sobrescribe con datos aleatorios antes de borrar. El contenido ya está cifrado, así
    que además del borrado físico rige el borrado criptográfico (rotación de clave)."""
    if not path.exists():
        return
    size = path.stat().st_size
    with path.open("r+b") as fh:
        fh.write(os.urandom(size))
        fh.flush()
        os.fsync(fh.fileno())
    path.unlink()
