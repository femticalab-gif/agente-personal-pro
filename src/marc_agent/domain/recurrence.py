"""Recurrencias (RRULE) con atajos legibles."""

from __future__ import annotations

from datetime import datetime

from dateutil.rrule import rrulestr

from marc_agent.tools.base import ToolError

SHORTCUTS = {
    "daily": "FREQ=DAILY",
    "diario": "FREQ=DAILY",
    "weekly": "FREQ=WEEKLY",
    "semanal": "FREQ=WEEKLY",
    "monthly": "FREQ=MONTHLY",
    "mensual": "FREQ=MONTHLY",
    "yearly": "FREQ=YEARLY",
    "anual": "FREQ=YEARLY",
    "weekdays": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
    "laborables": "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
}


def normalize_rule(rule: str | None) -> str | None:
    if rule is None or not rule.strip():
        return None
    text = rule.strip()
    text = SHORTCUTS.get(text.lower(), text)
    if text.upper().startswith("RRULE:"):
        text = text[6:]
    text = text.upper()
    if "FREQ=" not in text:
        raise ToolError(
            f"Recurrencia no válida: {rule!r}. Usa RRULE (FREQ=...) o daily/weekly/monthly."
        )
    # Validación sintáctica.
    rrulestr(text, dtstart=datetime(2000, 1, 1))
    return text


def next_after(rule: str, dtstart: datetime, after: datetime) -> datetime | None:
    """Siguiente ocurrencia estrictamente posterior a ``after`` (en la zona de ``dtstart``)."""
    rr = rrulestr(rule, dtstart=dtstart)
    candidate = rr.after(after.astimezone(dtstart.tzinfo), inc=False)
    return candidate
