"""Allowlist de un único usuario y rate limit anti-spam."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from marc_agent.audit import AuditLog
from marc_agent.gateway.api import Incoming

log = logging.getLogger(__name__)


@dataclass
class TokenBucket:
    rate_per_s: float
    capacity: float
    tokens: float = field(init=False)
    updated: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.tokens = self.capacity

    def take(self, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate_per_s)
        self.updated = now
        if self.tokens >= 1:
            self.tokens -= 1
            return True
        return False


class AccessGuard:
    """Solo pasa el ``TELEGRAM_USER_ID`` configurado, en chat privado y bajo el rate limit."""

    def __init__(self, allowed_user_id: int, per_minute: int, burst: int, audit: AuditLog) -> None:
        if allowed_user_id <= 0:
            raise ValueError("TELEGRAM_USER_ID inválido")
        self._allowed = allowed_user_id
        self._bucket = TokenBucket(per_minute / 60.0, float(burst))
        self._audit = audit
        self._rejected_log: dict[str, float] = {}
        self._last_throttle_notice = 0.0

    def is_allowed_user(self, update: Incoming) -> bool:
        return update.user_id == self._allowed and update.chat_type == "private"

    def check(self, update: Incoming) -> str:
        """Devuelve ``ok``, ``unauthorized`` o ``rate_limited``."""
        if not self.is_allowed_user(update):
            self._log_rejected(update)
            return "unauthorized"
        # Las pulsaciones de botones no consumen cupo (aprobaciones nunca se bloquean).
        if update.kind != "callback" and not self._bucket.take():
            self._audit.event("rate_limited", logging.WARNING)
            return "rate_limited"
        return "ok"

    def should_notify_throttle(self) -> bool:
        now = time.monotonic()
        if now - self._last_throttle_notice > 60:
            self._last_throttle_notice = now
            return True
        return False

    def _log_rejected(self, update: Incoming) -> None:
        who = self._audit.digest({"user": update.user_id, "chat": update.chat_id})
        now = time.monotonic()
        # Como mucho un registro por remitente y minuto (evita inundar el log).
        if now - self._rejected_log.get(who, 0) > 60:
            self._rejected_log[who] = now
            self._audit.event(
                "unauthorized_update",
                logging.WARNING,
                sender=who,
                chat_type=update.chat_type,
                kind=update.kind,
            )
