"""Abstracción mínima de la API de Telegram (para poder testear sin red) y su adaptador."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from marc_agent.notify import Keyboard
from marc_agent.util import truncate

TG_LIMIT = 4000


@dataclass
class Incoming:
    """Update normalizado (independiente de python-telegram-bot)."""

    update_id: int
    user_id: int
    chat_id: int
    chat_type: str
    kind: str  # text | command | voice | photo | document | callback | other
    text: str = ""
    command: str = ""
    args: list[str] = field(default_factory=list)
    file_id: str = ""
    mime: str = ""
    file_name: str = ""
    file_size: int = 0
    duration: int = 0
    callback_id: str = ""
    callback_data: str = ""
    message_id: int = 0
    forwarded: bool = False


class TelegramApi(Protocol):
    async def send_message(
        self, chat_id: int, text: str, buttons: Keyboard | None = None
    ) -> int: ...

    async def edit_message(
        self, chat_id: int, message_id: int, text: str, buttons: Keyboard | None = None
    ) -> None: ...

    async def delete_message(self, chat_id: int, message_id: int) -> None: ...

    async def send_document(
        self, chat_id: int, data: bytes, filename: str, caption: str = ""
    ) -> None: ...

    async def answer_callback(self, callback_id: str, text: str = "") -> None: ...

    async def download(self, file_id: str) -> bytes: ...

    async def typing(self, chat_id: int) -> None: ...


def split_text(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Parte un texto largo en trozos aptos para Telegram, preferiblemente por líneas."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for raw_line in text.splitlines(keepends=True):
        line = raw_line
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks


class TelegramNotifier:
    """``Notifier`` hacia el único chat autorizado."""

    def __init__(self, api: TelegramApi, chat_id: int) -> None:
        self._api = api
        self._chat_id = chat_id

    async def send(self, text: str, buttons: Keyboard | None = None) -> int | None:
        chunks = split_text(text)
        message_id: int | None = None
        for idx, chunk in enumerate(chunks):
            last = idx == len(chunks) - 1
            message_id = await self._api.send_message(
                self._chat_id, chunk, buttons if last else None
            )
        return message_id

    async def edit(self, message_id: int, text: str, buttons: Keyboard | None = None) -> None:
        await self._api.edit_message(self._chat_id, message_id, truncate(text, TG_LIMIT), buttons)

    async def send_document(self, data: bytes, filename: str, caption: str = "") -> None:
        await self._api.send_document(self._chat_id, data, filename, truncate(caption, 1000))
