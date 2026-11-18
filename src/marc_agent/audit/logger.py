"""Log JSON estructurado con redacción y rotación.

Principio: el log de auditoría registra *qué* pasó (tool, categoría, decisión, duración,
claves de argumentos y un digest HMAC de los valores), nunca *el contenido*. Así sirve
para auditar sin convertirse en una copia en claro de correos, salud o finanzas.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from marc_agent.audit.redact import Redactor
from marc_agent.crypto import canonical_json
from marc_agent.util import iso, utcnow

_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": iso(utcnow()),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(self._redactor.value(payload), ensure_ascii=False, default=str)


class _RedactingFilter(logging.Filter):
    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        # Evita que librerías de terceros (httpx, telegram) filtren URLs con tokens.
        if isinstance(record.msg, str):
            record.msg = self._redactor.text(record.msg)
        if record.args:
            record.args = tuple(
                self._redactor.text(a) if isinstance(a, str) else a
                for a in (record.args if isinstance(record.args, tuple) else (record.args,))
            )
        return True


def setup_logging(
    log_dir: Path | None,
    redactor: Redactor,
    level: int = logging.INFO,
    max_bytes: int = 10 * 1024 * 1024,
    backups: int = 10,
) -> None:
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    formatter = JsonFormatter(redactor)
    flt = _RedactingFilter(redactor)
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    stream.addFilter(flt)
    root.addHandler(stream)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_dir / "audit.jsonl", maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(flt)
        root.addHandler(file_handler)
    root.setLevel(level)
    # httpx loguea URLs completas (la API de Telegram lleva el token en la URL).
    for noisy in ("httpx", "httpcore", "telegram", "apscheduler", "anthropic", "claude_agent_sdk"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


class AuditLog:
    """Eventos de auditoría de alto nivel."""

    def __init__(self, digest_key: bytes, logger_name: str = "marc.audit") -> None:
        self._key = digest_key
        self._log = logging.getLogger(logger_name)

    def digest(self, value: Any) -> str:
        raw = canonical_json(value).encode("utf-8")
        return hmac.new(self._key, raw, hashlib.sha256).hexdigest()[:16]

    def event(self, event: str, level: int = logging.INFO, **fields: Any) -> None:
        self._log.log(level, event, extra={"event": event, **fields})

    def tool_call(
        self,
        *,
        tool: str,
        category: str,
        decision: str,
        args: dict[str, Any],
        reason: str = "",
        duration_ms: int | None = None,
        agent_id: str | None = None,
        tainted: bool = False,
        ok: bool | None = None,
    ) -> None:
        self.event(
            "tool_call",
            tool=tool,
            category=category,
            decision=decision,
            reason=reason,
            arg_keys=sorted(args.keys()),
            args_digest=self.digest(args),
            duration_ms=duration_ms,
            agent_id=agent_id,
            tainted=tainted,
            ok=ok,
        )
