"""Redacción de secretos y datos sensibles antes de escribir logs.

Dos capas:
1. Claves de diccionario sospechosas (``token``, ``secret``, ``password``...) ⇒ ``[REDACTED]``.
2. Patrones en cualquier texto: tokens de Telegram, claves de Anthropic/Google, bearer,
   IBAN, tarjetas (Luhn) y los valores exactos de los secretos cargados.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

REDACTED = "[REDACTED]"

_SENSITIVE_KEYS = re.compile(
    r"(token|secret|passw|api[_-]?key|authorization|cookie|credential|refresh|"
    r"private|iban|card|tarjeta|cvv|pin|session_key|fernet|data_key|backup_key|export_key)",
    re.IGNORECASE,
)

_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),  # token bot Telegram
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),  # Anthropic
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"),  # Google API key
    re.compile(r"\bya29\.[0-9A-Za-z_\-\.]+"),  # Google access token
    re.compile(r"\b1//[0-9A-Za-z_\-]{20,}"),  # Google refresh token
    re.compile(r"GOCSPX-[0-9A-Za-z_\-]+"),  # Google client secret
    re.compile(r"(?i)bearer\s+[A-Za-z0-9_\-\.=]+"),
    re.compile(r"\btskey-[A-Za-z0-9\-]+"),  # Tailscale
    re.compile(r"\b[A-Za-z0-9_\-]{43}=\b"),  # claves Fernet
    re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}(?:[ ]?[A-Z0-9]{1,4})?\b"),  # IBAN
]

_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")


def _luhn_ok(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


class Redactor:
    def __init__(self, secret_values: Iterable[str] = ()) -> None:
        self._secrets = sorted(
            {s for s in secret_values if s and len(s) >= 8}, key=len, reverse=True
        )

    def add_secrets(self, values: Iterable[str]) -> None:
        merged = set(self._secrets) | {v for v in values if v and len(v) >= 8}
        self._secrets = sorted(merged, key=len, reverse=True)

    def text(self, value: str) -> str:
        out = value
        for secret in self._secrets:
            if secret in out:
                out = out.replace(secret, REDACTED)
        for pattern in _PATTERNS:
            out = pattern.sub(REDACTED, out)

        def _card(match: re.Match[str]) -> str:
            digits = re.sub(r"\D", "", match.group(0))
            return REDACTED if 13 <= len(digits) <= 19 and _luhn_ok(digits) else match.group(0)

        return _CARD.sub(_card, out)

    def value(self, obj: Any, depth: int = 0) -> Any:
        if depth > 8:
            return "[...]"
        if isinstance(obj, str):
            return self.text(obj)
        if isinstance(obj, dict):
            return {
                str(k): (REDACTED if _SENSITIVE_KEYS.search(str(k)) else self.value(v, depth + 1))
                for k, v in obj.items()
            }
        if isinstance(obj, (list, tuple, set)):
            return [self.value(v, depth + 1) for v in obj]
        if isinstance(obj, (int, float, bool)) or obj is None:
            return obj
        return self.text(str(obj))
