"""/export (volcado completo cifrado) y /borrar_todo (borrado irrecuperable)."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any

from marc_agent.crypto import StreamEncryptor
from marc_agent.db.schema import PERSONAL_TABLES
from marc_agent.services import Services

EXPORT_PURPOSE = "export"
_SKIP_TABLES = {"session_entries", "session_meta", "notifications_sent"}
_MAX_FILES_BYTES = 30 * 1024 * 1024
_ENCRYPTED_COLUMNS = {"payload", "summary"}


def build_export(svc: Services) -> dict[str, Any]:
    data: dict[str, Any] = {
        "formato": 1,
        "generado": datetime.now(UTC).isoformat(timespec="seconds"),
        "perfil": svc.config.profile.model_dump(),
        "tablas": {},
    }
    for table in PERSONAL_TABLES:
        if table in _SKIP_TABLES:
            continue
        rows = svc.db.query(f"SELECT * FROM {table}")
        out = []
        for row in rows:
            item: dict[str, Any] = {}
            for key in row.keys():  # noqa: SIM118 - sqlite3.Row no es un dict
                value = row[key]
                if key in _ENCRYPTED_COLUMNS and value is not None:
                    item[key] = svc.db.dec(value)
                else:
                    item[key] = value
            out.append(item)
        data["tablas"][table] = out
    files: dict[str, Any] = {}
    total = 0
    for doc in svc.vault.list_docs(limit=10_000):
        raw, mime, name = svc.vault.read_file(doc["id"])
        if total + len(raw) > _MAX_FILES_BYTES:
            files[doc["id"]] = {"omitido": "límite de tamaño del export", "nombre": name}
            continue
        total += len(raw)
        files[doc["id"]] = {"nombre": name, "mime": mime, "base64": base64.b64encode(raw).decode()}
    data["ficheros_vault"] = files
    return data


def export_encrypted(svc: Services, export_key: str) -> tuple[bytes, str]:
    payload = json.dumps(build_export(svc), ensure_ascii=False, default=str).encode("utf-8")
    encrypted = StreamEncryptor(export_key, EXPORT_PURPOSE).encrypt_bytes(payload)
    name = f"marc-export-{datetime.now(UTC):%Y%m%d-%H%M%S}.json.enc"
    return encrypted, name


def decrypt_export(data: bytes, export_key: str) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(
        StreamEncryptor(export_key, EXPORT_PURPOSE).decrypt_bytes(data)
    )
    return result


def wipe_everything(svc: Services, backups: Any) -> dict[str, int]:
    """Borrado total e irrecuperable de los datos personales.

    * Ficheros del vault: sobrescritos con datos aleatorios y borrados.
    * SQLite: ``DELETE`` con ``secure_delete=ON`` + ``VACUUM`` (sin restos en páginas libres).
    * Backups: borrados (contienen los mismos datos).
    * Índice de búsqueda en memoria: vaciado.
    Se conservan la configuración, los tokens OAuth y el log de auditoría (sin contenido).
    """
    files = svc.vault.wipe_files()
    svc.db.wipe_personal_data()
    removed_backups = backups.wipe_all() if backups is not None else 0
    svc.index.clear()
    svc.audit.event("wipe_all", vault_files=files, backups=removed_backups)
    return {"ficheros_vault": files, "backups": removed_backups}
