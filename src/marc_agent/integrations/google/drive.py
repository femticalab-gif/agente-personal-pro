"""Cliente de Google Drive en solo lectura."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from marc_agent.integrations import IntegrationError
from marc_agent.integrations.docparse import MAX_BYTES, extract_text
from marc_agent.integrations.google.auth import GoogleApi

BASE = "https://www.googleapis.com/drive/v3/files"

_EXPORTS = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


@dataclass
class DriveFile:
    id: str
    name: str
    mime: str
    modified: str
    size: int

    def to_public(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "mime": self.mime, "modified": self.modified}


def _escape(query: str) -> str:
    return query.replace("\\", "\\\\").replace("'", "\\'")


class DriveClient:
    def __init__(self, api: GoogleApi) -> None:
        self._api = api

    async def search(self, query: str, max_results: int = 10) -> list[DriveFile]:
        q = _escape(query.strip())
        params = {
            "q": f"(name contains '{q}' or fullText contains '{q}') and trashed = false",
            "pageSize": max(1, min(max_results, 50)),
            "fields": "files(id,name,mimeType,modifiedTime,size)",
            "orderBy": "modifiedTime desc",
        }
        data = await self._api.json("GET", BASE, params=params)
        return [
            DriveFile(
                id=f["id"],
                name=f.get("name", ""),
                mime=f.get("mimeType", ""),
                modified=f.get("modifiedTime", ""),
                size=int(f.get("size", 0) or 0),
            )
            for f in data.get("files", [])
        ]

    async def read_text(self, file_id: str, max_chars: int = 30_000) -> tuple[DriveFile, str]:
        meta = await self._api.json(
            "GET", f"{BASE}/{file_id}", params={"fields": "id,name,mimeType,modifiedTime,size"}
        )
        info = DriveFile(
            id=meta["id"],
            name=meta.get("name", ""),
            mime=meta.get("mimeType", ""),
            modified=meta.get("modifiedTime", ""),
            size=int(meta.get("size", 0) or 0),
        )
        if info.mime in _EXPORTS:
            response = await self._api.request(
                "GET", f"{BASE}/{file_id}/export", params={"mimeType": _EXPORTS[info.mime]}
            )
            return info, response.text[:max_chars]
        if info.mime.startswith("application/vnd.google-apps"):
            raise IntegrationError(f"Tipo de Google no exportable: {info.mime}")
        if info.size > MAX_BYTES:
            raise IntegrationError("Fichero demasiado grande para leerlo (>20 MB)")
        response = await self._api.request("GET", f"{BASE}/{file_id}", params={"alt": "media"})
        return info, extract_text(response.content, info.mime, max_chars)
