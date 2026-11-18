"""Adaptador python-telegram-bot: LONG POLLING (sin webhook, cero puertos de entrada)."""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest, RetryAfter, TelegramError
from telegram.ext import Application, ApplicationBuilder, ContextTypes, TypeHandler
from telegram.request import HTTPXRequest

from marc_agent.gateway.api import Incoming
from marc_agent.notify import Keyboard

log = logging.getLogger(__name__)

_NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def _markup(buttons: Keyboard | None) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(b.text, callback_data=b.data) for b in row] for row in buttons]
    )


class PTBApi:
    def __init__(self, application: Application) -> None:  # type: ignore[type-arg]
        self._app = application

    @property
    def _bot(self):  # type: ignore[no-untyped-def]
        return self._app.bot

    async def _retry(self, func: Callable[[], Awaitable[object]]) -> object:
        try:
            return await func()
        except RetryAfter as exc:
            delay = (
                exc.retry_after
                if isinstance(exc.retry_after, (int, float))
                else exc.retry_after.total_seconds()
            )
            await asyncio.sleep(float(delay) + 0.5)
            return await func()

    async def send_message(self, chat_id: int, text: str, buttons: Keyboard | None = None) -> int:
        msg = await self._retry(
            lambda: self._bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=_markup(buttons),
                link_preview_options=_NO_PREVIEW,
            )
        )
        return int(msg.message_id)  # type: ignore[attr-defined]

    async def edit_message(
        self, chat_id: int, message_id: int, text: str, buttons: Keyboard | None = None
    ) -> None:
        try:
            await self._retry(
                lambda: self._bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    reply_markup=_markup(buttons),
                    link_preview_options=_NO_PREVIEW,
                )
            )
        except BadRequest as exc:
            if "not modified" not in str(exc).lower():
                raise

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        try:
            await self._bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError:
            log.debug("delete_failed")

    async def send_document(
        self, chat_id: int, data: bytes, filename: str, caption: str = ""
    ) -> None:
        await self._retry(
            lambda: self._bot.send_document(
                chat_id=chat_id,
                document=InputFile(io.BytesIO(data), filename=filename),
                caption=caption,
            )
        )

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            await self._bot.answer_callback_query(callback_query_id=callback_id, text=text or None)
        except TelegramError:
            log.debug("answer_callback_failed")

    async def download(self, file_id: str) -> bytes:
        tg_file = await self._bot.get_file(file_id)
        return bytes(await tg_file.download_as_bytearray())

    async def typing(self, chat_id: int) -> None:
        await self._bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)


def _seconds(value: int | timedelta | None) -> int:
    if isinstance(value, timedelta):
        return int(value.total_seconds())
    return int(value or 0)


def to_incoming(update: Update) -> Incoming | None:
    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        return None

    def make(**fields: Any) -> Incoming:
        return Incoming(
            update_id=update.update_id,
            user_id=user.id,
            chat_id=chat.id,
            chat_type=str(chat.type),
            **fields,
        )

    if update.callback_query is not None:
        cq = update.callback_query
        message_id = cq.message.message_id if cq.message is not None else 0
        return make(
            kind="callback",
            callback_id=cq.id,
            callback_data=cq.data or "",
            message_id=message_id,
        )
    msg = update.message
    if msg is None:
        return make(kind="other")
    forwarded = msg.forward_origin is not None
    if msg.text is not None:
        text = msg.text
        if text.startswith("/") and not forwarded:
            head, *rest = text[1:].split()
            command = head.split("@", 1)[0].lower()
            return make(
                kind="command",
                text=text,
                command=command,
                args=rest,
                message_id=msg.message_id,
            )
        return make(kind="text", text=text, forwarded=forwarded, message_id=msg.message_id)
    if msg.voice is not None or msg.audio is not None:
        media = msg.voice if msg.voice is not None else msg.audio
        if media is None:  # pragma: no cover - comprobado arriba
            return None
        return make(
            kind="voice",
            file_id=media.file_id,
            mime=media.mime_type or "audio/ogg",
            duration=_seconds(media.duration),
            file_size=int(media.file_size or 0),
            forwarded=forwarded,
            message_id=msg.message_id,
        )
    if msg.photo:
        photo = msg.photo[-1]
        return make(
            kind="photo",
            file_id=photo.file_id,
            mime="image/jpeg",
            file_size=int(photo.file_size or 0),
            text=msg.caption or "",
            forwarded=forwarded,
            message_id=msg.message_id,
        )
    if msg.document is not None:
        doc = msg.document
        return make(
            kind="document",
            file_id=doc.file_id,
            mime=doc.mime_type or "application/octet-stream",
            file_name=doc.file_name or "",
            file_size=int(doc.file_size or 0),
            text=msg.caption or "",
            forwarded=forwarded,
            message_id=msg.message_id,
        )
    return make(kind="other", message_id=msg.message_id)


class TelegramRunner:
    def __init__(self, token: str, proxy_url: str | None) -> None:
        request = HTTPXRequest(connection_pool_size=16, proxy=proxy_url, read_timeout=30)
        updates_request = HTTPXRequest(connection_pool_size=2, proxy=proxy_url, read_timeout=60)
        self.application: Application = (  # type: ignore[type-arg]
            ApplicationBuilder()
            .token(token)
            .request(request)
            .get_updates_request(updates_request)
            .concurrent_updates(8)
            .build()
        )
        self.api = PTBApi(self.application)

    def on_update(self, handler: Callable[[Incoming], Awaitable[None]]) -> None:
        async def _handle(update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
            incoming = to_incoming(update)
            if incoming is not None:
                await handler(incoming)

        async def _error(_update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
            log.warning(
                "telegram_error",
                extra={"event": "telegram_error", "error": type(context.error).__name__},
            )

        self.application.add_handler(TypeHandler(Update, _handle))
        self.application.add_error_handler(_error)

    async def start(self, commands: list[tuple[str, str]]) -> None:
        await self.application.initialize()
        try:
            await self.application.bot.set_my_commands([BotCommand(c, d) for c, d in commands])
        except TelegramError:
            log.warning("set_commands_failed")
        await self.application.start()
        if self.application.updater is None:
            raise RuntimeError("La aplicación de Telegram no tiene updater (long polling)")
        await self.application.updater.start_polling(
            allowed_updates=["message", "callback_query"], drop_pending_updates=False, timeout=50
        )

    async def stop(self) -> None:
        if self.application.updater is not None and self.application.updater.running:
            await self.application.updater.stop()
        if self.application.running:
            await self.application.stop()
        await self.application.shutdown()
