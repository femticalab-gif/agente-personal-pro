"""Recordatorios únicos, recurrentes (RRULE) o ligados a un vencimiento.

Ejemplo de recordatorio ligado: «avísame el día antes de la cuota» ⇒ ``due_id`` del
vencimiento de la cuota y ``offset_days=-1``. Cuando el vencimiento avanza al mes siguiente,
el recordatorio se reprograma solo.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from marc_agent.db import Database
from marc_agent.domain.recurrence import next_after, normalize_rule
from marc_agent.tools.base import ToolError
from marc_agent.util import iso, new_id, parse_dt, utcnow

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class RemindersService:
    def __init__(self, db: Database, tz: ZoneInfo) -> None:
        self._db = db
        self._tz = tz

    def _linked_time(self, due_date: str, offset_days: int, hhmm: str) -> datetime:
        match = _HHMM.match(hhmm)
        if not match:
            raise ToolError("Hora en formato HH:MM")
        d = datetime.fromisoformat(due_date).date() + timedelta(days=offset_days)
        return datetime(
            d.year, d.month, d.day, int(match.group(1)), int(match.group(2)), tzinfo=self._tz
        )

    def create(
        self,
        text: str,
        at: str | None = None,
        recurrence: str | None = None,
        due_id: str | None = None,
        offset_days: int = 0,
        time_of_day: str = "09:00",
    ) -> dict[str, Any]:
        if not text.strip():
            raise ToolError("El recordatorio necesita texto")
        now = utcnow()
        payload: dict[str, Any] = {"texto": text.strip(), "hora": time_of_day}
        rule: str | None = None
        if due_id:
            due = self._db.query_one("SELECT due_date, active FROM due_items WHERE id=?", (due_id,))
            if due is None or not due["active"]:
                raise ToolError(f"No existe el vencimiento activo {due_id}")
            if not -60 <= offset_days <= 0:
                raise ToolError("offset_days entre -60 y 0 (días antes del vencimiento)")
            next_run = self._linked_time(due["due_date"], offset_days, time_of_day)
            payload["ultimo_vencimiento"] = due["due_date"]
        else:
            if not at:
                raise ToolError("Indica 'at' (fecha/hora ISO) o un 'due_id' de vencimiento")
            first = parse_dt(at, self._tz)
            rule = normalize_rule(recurrence)
            payload["primera"] = first.isoformat()
            next_run = first
            if first <= now:
                if rule is None:
                    raise ToolError("La fecha del recordatorio ya ha pasado")
                nxt = next_after(rule, first, now)
                if nxt is None:
                    raise ToolError("La recurrencia no tiene ocurrencias futuras")
                next_run = nxt
        reminder_id = new_id("r")
        stamp = iso(now)
        self._db.execute(
            "INSERT INTO reminders(id, created_at, updated_at, active, next_run_at, recurrence, "
            "due_id, offset_days, payload) VALUES(?,?,?,1,?,?,?,?,?)",
            (
                reminder_id,
                stamp,
                stamp,
                iso(next_run),
                rule,
                due_id,
                offset_days if due_id else None,
                self._db.enc(payload),
            ),
        )
        return self.get(reminder_id)

    def _public(self, row: Any) -> dict[str, Any]:
        payload = self._db.dec(row["payload"])
        nxt = row["next_run_at"]
        return {
            "id": row["id"],
            "activo": bool(row["active"]),
            "proximo": datetime.fromisoformat(nxt).astimezone(self._tz).isoformat()
            if nxt
            else None,
            "recurrencia": row["recurrence"],
            "vencimiento": row["due_id"],
            "dias_offset": row["offset_days"],
            "texto": payload["texto"],
        }

    def get(self, reminder_id: str) -> dict[str, Any]:
        row = self._db.query_one("SELECT * FROM reminders WHERE id=?", (reminder_id,))
        if row is None:
            raise ToolError(f"No existe el recordatorio {reminder_id}")
        return self._public(row)

    def list_active(self, active_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM reminders" + (" WHERE active=1" if active_only else "")
        return [self._public(r) for r in self._db.query(sql + " ORDER BY next_run_at")]

    def cancel(self, reminder_id: str) -> bool:
        cur = self._db.execute(
            "UPDATE reminders SET active=0, updated_at=? WHERE id=?", (iso(utcnow()), reminder_id)
        )
        return cur.rowcount == 1

    def due_now(self, now: datetime) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM reminders WHERE active=1 AND next_run_at IS NOT NULL AND next_run_at<=?",
            (iso(now),),
        )
        return [self._public(r) for r in rows]

    def mark_fired(self, reminder_id: str, now: datetime) -> None:
        row = self._db.query_one("SELECT * FROM reminders WHERE id=?", (reminder_id,))
        if row is None:
            return
        payload = self._db.dec(row["payload"])
        if row["recurrence"]:
            first = datetime.fromisoformat(payload["primera"])
            nxt = next_after(row["recurrence"], first, now)
            self._db.execute(
                "UPDATE reminders SET next_run_at=?, active=?, updated_at=? WHERE id=?",
                (iso(nxt) if nxt else None, 1 if nxt else 0, iso(now), reminder_id),
            )
        elif row["due_id"]:
            # Espera a que el vencimiento avance para reprogramarse (refresh_linked).
            self._db.execute(
                "UPDATE reminders SET next_run_at=NULL, updated_at=? WHERE id=?",
                (iso(now), reminder_id),
            )
        else:
            self._db.execute(
                "UPDATE reminders SET active=0, next_run_at=NULL, updated_at=? WHERE id=?",
                (iso(now), reminder_id),
            )

    def refresh_linked(self) -> int:
        """Reprograma recordatorios ligados cuyo vencimiento ha cambiado de fecha."""
        changed = 0
        rows = self._db.query("SELECT * FROM reminders WHERE active=1 AND due_id IS NOT NULL")
        for row in rows:
            due = self._db.query_one(
                "SELECT due_date, active FROM due_items WHERE id=?", (row["due_id"],)
            )
            if due is None or not due["active"]:
                self._db.execute("UPDATE reminders SET active=0 WHERE id=?", (row["id"],))
                changed += 1
                continue
            payload = self._db.dec(row["payload"])
            if payload.get("ultimo_vencimiento") == due["due_date"] and row["next_run_at"] is None:
                continue
            if payload.get("ultimo_vencimiento") != due["due_date"]:
                payload["ultimo_vencimiento"] = due["due_date"]
                next_run = self._linked_time(
                    due["due_date"], int(row["offset_days"] or 0), payload.get("hora", "09:00")
                )
                self._db.execute(
                    "UPDATE reminders SET next_run_at=?, payload=?, updated_at=? WHERE id=?",
                    (iso(next_run), self._db.enc(payload), iso(utcnow()), row["id"]),
                )
                changed += 1
        return changed
