"""Respuesta en streaming: edita el mensaje de Telegram progresivamente.

* Throttling de ediciones (Telegram limita la frecuencia).
* Línea de estado con la herramienta en uso y un contador para tareas largas.
* El texto final sustituye al borrador en streaming (puede ser más corto: el streaming
  incluye la narración intermedia entre llamadas a herramientas).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from marc_agent.gateway.api import TG_LIMIT, TelegramApi, split_text

log = logging.getLogger(__name__)

_FRIENDLY = {
    "gmail": "correo",
    "calendar": "calendario",
    "drive": "Drive",
    "vault": "documentos",
    "finance": "finanzas",
    "health": "salud",
    "task": "tareas",
    "note": "notas",
    "reminder": "recordatorios",
    "contact": "contactos",
    "memory": "memoria",
    "web": "búsqueda web",
    "weather": "clima",
    "Agent": "subagentes",
    "Task": "subagentes",
}


def friendly_tool(name: str) -> str:
    short = name.removeprefix("mcp__marc__")
    for prefix, label in _FRIENDLY.items():
        if short.startswith(prefix):
            return label
    return short


class TelegramStreamSink:
    def __init__(
        self,
        api: TelegramApi,
        chat_id: int,
        edit_interval_s: float = 1.2,
        progress_after_s: float = 15.0,
    ) -> None:
        self._api = api
        self._chat_id = chat_id
        self._interval = edit_interval_s
        self._progress_after = progress_after_s
        self._message_id: int | None = None
        self._buffer = ""
        self._shown = ""
        self._status = ""
        self._status_line = ""
        self._last_edit = 0.0
        self._started = time.monotonic()
        self._tools = 0
        self._ticker: asyncio.Task[None] | None = None
        self._closed = False
        self._lock = asyncio.Lock()

    @property
    def closed(self) -> bool:
        return self._closed

    def is_closed(self) -> bool:
        return self._closed

    async def start(self) -> None:
        with contextlib.suppress(Exception):
            await self._api.typing(self._chat_id)
        self._ticker = asyncio.create_task(self._tick())

    async def _tick(self) -> None:
        try:
            while not self.is_closed():
                await asyncio.sleep(self._progress_after)
                if self.is_closed():  # puede haberse cerrado durante la espera
                    return
                elapsed = int(time.monotonic() - self._started)
                label = self._status or "trabajando"
                self._status_line = f"⏳ {label}… {elapsed} s"
                await self._render(force=True)
        except asyncio.CancelledError:
            return

    def _compose(self) -> str:
        body = (
            self._buffer[-(TG_LIMIT - 200) :]
            if len(self._buffer) > TG_LIMIT - 200
            else self._buffer
        )
        if self._status_line:
            return f"{body}\n\n{self._status_line}".strip()
        return body or "…"

    async def _render(self, force: bool = False) -> None:
        async with self._lock:
            if self._closed and not force:
                return
            now = time.monotonic()
            if not force and now - self._last_edit < self._interval:
                return
            text = self._compose()
            if text == self._shown:
                return
            try:
                if self._message_id is None:
                    self._message_id = await self._api.send_message(self._chat_id, text)
                else:
                    await self._api.edit_message(self._chat_id, self._message_id, text)
                self._shown = text
                self._last_edit = now
            except Exception:
                log.debug("stream_edit_failed")

    async def on_text_delta(self, text: str) -> None:
        self._buffer += text
        await self._render()

    async def on_tool_use(self, tool_name: str) -> None:
        self._tools += 1
        self._status = f"usando {friendly_tool(tool_name)}"
        self._status_line = f"⏳ {self._status}…"
        await self._render()

    async def _close(self) -> None:
        self._closed = True
        if self._ticker is not None and not self._ticker.done():
            # Solo se cancela: esperar al ticker suprimiendo CancelledError podría tragarse
            # una cancelación dirigida a la tarea actual (p. ej. al parar el gateway).
            self._ticker.cancel()

    async def finish(self, text: str) -> None:
        await self._close()
        chunks = split_text(text or "(sin respuesta)")
        async with self._lock:
            try:
                if self._message_id is None:
                    await self._api.send_message(self._chat_id, chunks[0])
                elif chunks[0] != self._shown:
                    await self._api.edit_message(self._chat_id, self._message_id, chunks[0])
                for chunk in chunks[1:]:
                    await self._api.send_message(self._chat_id, chunk)
            except Exception:
                log.warning("stream_finish_failed", extra={"event": "stream_finish_failed"})

    async def fail(self, text: str) -> None:
        await self.finish(text)
