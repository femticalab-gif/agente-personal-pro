"""Utilidades comunes: ids, fechas y texto."""

from __future__ import annotations

import re
import secrets
import unicodedata
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo


def new_id(prefix: str) -> str:
    """Id corto y legible (p. ej. ``t-3f9a2c0b1d7e``) que el usuario puede citar en el chat."""
    return f"{prefix}-{secrets.token_hex(6)}"


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("Se requiere datetime con zona horaria")
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse_dt(value: str, tz: ZoneInfo) -> datetime:
    """Parsea ISO-8601. Si no trae zona, se interpreta en la zona del usuario."""
    text = value.strip().replace("Z", "+00:00")
    if len(text) == 10:
        d = date.fromisoformat(text)
        return datetime(d.year, d.month, d.day, tzinfo=tz)
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def parse_date(value: str) -> date:
    return date.fromisoformat(value.strip()[:10])


def local_now(tz: ZoneInfo) -> datetime:
    return utcnow().astimezone(tz)


def fmt_local(dt: datetime, tz: ZoneInfo, with_date: bool = True) -> str:
    local = dt.astimezone(tz)
    days = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
    if with_date:
        return f"{days[local.weekday()]} {local:%d/%m %H:%M}"
    return f"{local:%H:%M}"


def day_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    return start, start + timedelta(days=1)


def month_key(d: date | datetime) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def strip_accents(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text)
    return "".join(c for c in normalized if not unicodedata.combining(c))


def normalize(text: str) -> str:
    """Minúsculas sin acentos ni espacios repetidos (para comparar y buscar)."""
    return re.sub(r"\s+", " ", strip_accents(text).lower()).strip()


_INVISIBLE_CODEPOINTS = (
    0x200B,
    0x200C,
    0x200D,
    0x200E,
    0x200F,
    0x2060,
    0x2061,
    0x2062,
    0x2063,
    0x2064,
    0xFEFF,
)
_ZERO_WIDTH = re.compile("[" + "".join(map(chr, _INVISIBLE_CODEPOINTS)) + "]")


def remove_invisible(text: str) -> str:
    return _ZERO_WIDTH.sub("", text)


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def money(amount: float, currency: str = "EUR") -> str:
    symbol = {"EUR": "€", "USD": "$", "GBP": "£"}.get(currency.upper(), currency.upper())
    formatted = f"{amount:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"{formatted} {symbol}"
