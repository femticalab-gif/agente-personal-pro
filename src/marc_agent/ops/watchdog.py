"""Watchdog independiente (contenedor aparte) y healthcheck.

Si el agente lleva más de N minutos sin heartbeat, avisa a Marc por Telegram usando la Bot
API directamente (no depende del proceso del agente). Avisa también cuando se recupera.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime
from pathlib import Path

import httpx

from marc_agent.util import utcnow

log = logging.getLogger(__name__)


def heartbeat_age_s(path: Path) -> float | None:
    try:
        stamp = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return (utcnow() - stamp).total_seconds()


def healthcheck(path: Path, max_age_s: float = 300) -> bool:
    age = heartbeat_age_s(path)
    return age is not None and age <= max_age_s


async def send_telegram(http: httpx.AsyncClient, token: str, chat_id: int, text: str) -> bool:
    try:
        response = await http.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
        )
    except httpx.HTTPError:
        log.warning("watchdog_send_failed", extra={"event": "watchdog_send_failed"})
        return False
    return response.status_code == 200


class Watchdog:
    def __init__(
        self,
        heartbeat_file: Path,
        stale_after_s: float,
        http: httpx.AsyncClient,
        token: str,
        chat_id: int,
        interval_s: float = 60.0,
    ) -> None:
        self._file = heartbeat_file
        self._stale = stale_after_s
        self._http = http
        self._token = token
        self._chat_id = chat_id
        self._interval = interval_s
        self.alerted = False
        self._started = time.monotonic()

    async def check_once(self) -> str:
        age = heartbeat_age_s(self._file)
        # Margen de arranque: no alertar en los primeros minutos sin heartbeat.
        if age is None and time.monotonic() - self._started < self._stale:
            return "starting"
        stale = age is None or age > self._stale
        if stale and not self.alerted:
            minutes = "?" if age is None else f"{age / 60:.0f}"
            sent = await send_telegram(
                self._http,
                self._token,
                self._chat_id,
                f"🔴 Watchdog: el agente lleva {minutes} min sin heartbeat. Revisa el servidor "
                "(docker compose ps / logs).",
            )
            self.alerted = sent
            return "alerted" if sent else "alert_failed"
        if not stale and self.alerted:
            await send_telegram(
                self._http, self._token, self._chat_id, "🟢 Watchdog: el agente vuelve a responder."
            )
            self.alerted = False
            return "recovered"
        return "stale" if stale else "ok"

    async def run_forever(self) -> None:
        while True:
            try:
                await self.check_once()
            except Exception:
                log.exception("watchdog_loop_error")
            await asyncio.sleep(self._interval)
