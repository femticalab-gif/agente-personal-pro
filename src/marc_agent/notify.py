"""Interfaz de notificaciones hacia el usuario (implementada por el gateway de Telegram)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Button:
    text: str
    data: str


Keyboard = list[list[Button]]


class Notifier(Protocol):
    async def send(self, text: str, buttons: Keyboard | None = None) -> int | None:
        """Envía un mensaje al usuario. Devuelve el id de mensaje si existe."""
        ...

    async def edit(self, message_id: int, text: str, buttons: Keyboard | None = None) -> None: ...

    async def send_document(self, data: bytes, filename: str, caption: str = "") -> None: ...


class NullNotifier:
    """Notificador que descarta (útil antes de que el bot arranque)."""

    async def send(self, text: str, buttons: Keyboard | None = None) -> int | None:
        return None

    async def edit(self, message_id: int, text: str, buttons: Keyboard | None = None) -> None:
        return None

    async def send_document(self, data: bytes, filename: str, caption: str = "") -> None:
        return None
