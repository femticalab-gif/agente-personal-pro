"""Salud: citas, tratamientos y pautas, adherencia a la medicación y analíticas.

Todo el contenido clínico (nombres de medicamentos, dosis, especialidades, resultados) va
cifrado. En claro solo quedan fechas y estados necesarios para programar recordatorios.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from marc_agent.db import Database
from marc_agent.tools.base import ToolError
from marc_agent.util import iso, local_now, new_id, parse_date, parse_dt, utcnow

_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
INTAKE_STATUSES = ("tomada", "saltada", "pendiente")


class HealthService:
    def __init__(self, db: Database, tz: ZoneInfo) -> None:
        self._db = db
        self._tz = tz

    # -- citas -----------------------------------------------------------------------
    def add_appointment(
        self, when: str, specialty: str, doctor: str = "", place: str = "", notes: str = ""
    ) -> dict[str, Any]:
        at = parse_dt(when, self._tz)
        appt_id = new_id("a")
        payload = {"especialidad": specialty, "medico": doctor, "lugar": place, "notas": notes}
        now = iso(utcnow())
        self._db.execute(
            "INSERT INTO health_appointments(id, created_at, updated_at, at, status, payload) "
            "VALUES(?,?,?,?,?,?)",
            (appt_id, now, now, iso(at), "programada", self._db.enc(payload)),
        )
        return {"id": appt_id, "cuando": at.isoformat(), **payload}

    def _appt_public(self, row: Any) -> dict[str, Any]:
        at = datetime.fromisoformat(row["at"]).astimezone(self._tz)
        return {
            "id": row["id"],
            "cuando": at.isoformat(),
            "estado": row["status"],
            **self._db.dec(row["payload"]),
        }

    def list_appointments(
        self, start: datetime | None = None, end: datetime | None = None
    ) -> list[dict[str, Any]]:
        start = start or utcnow()
        end = end or start + timedelta(days=365)
        rows = self._db.query(
            "SELECT * FROM health_appointments WHERE at>=? AND at<? AND status='programada' "
            "ORDER BY at",
            (iso(start), iso(end)),
        )
        return [self._appt_public(r) for r in rows]

    def appointment_rows(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        return self.list_appointments(start, end)

    def cancel_appointment(self, appt_id: str) -> bool:
        cur = self._db.execute(
            "UPDATE health_appointments SET status='cancelada', updated_at=? WHERE id=?",
            (iso(utcnow()), appt_id),
        )
        return cur.rowcount == 1

    # -- tratamientos ----------------------------------------------------------------
    def add_treatment(
        self,
        medication: str,
        dose: str,
        times: list[str],
        start_date: str | None = None,
        end_date: str | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        if not times or any(not _TIME.match(t) for t in times):
            raise ToolError("Horas de toma en formato HH:MM (24 h), p. ej. ['08:00','20:00']")
        start = parse_date(start_date) if start_date else local_now(self._tz).date()
        end = parse_date(end_date) if end_date else None
        if end and end < start:
            raise ToolError("La fecha de fin es anterior al inicio")
        treatment_id = new_id("m")
        payload = {"medicamento": medication, "dosis": dose, "notas": notes}
        now = iso(utcnow())
        self._db.execute(
            "INSERT INTO treatments(id, created_at, updated_at, active, start_date, end_date, times, "
            "payload) VALUES(?,?,?,1,?,?,?,?)",
            (
                treatment_id,
                now,
                now,
                start.isoformat(),
                end.isoformat() if end else None,
                ",".join(sorted(times)),
                self._db.enc(payload),
            ),
        )
        return {
            "id": treatment_id,
            "horas": sorted(times),
            "inicio": start.isoformat(),
            "fin": end.isoformat() if end else None,
            **payload,
        }

    def _treatment_public(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "activo": bool(row["active"]),
            "horas": row["times"].split(","),
            "inicio": row["start_date"],
            "fin": row["end_date"],
            **self._db.dec(row["payload"]),
        }

    def list_treatments(self, active_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM treatments" + (" WHERE active=1" if active_only else "")
        return [self._treatment_public(r) for r in self._db.query(sql + " ORDER BY start_date")]

    def get_treatment(self, treatment_id: str) -> dict[str, Any]:
        row = self._db.query_one("SELECT * FROM treatments WHERE id=?", (treatment_id,))
        if row is None:
            raise ToolError(f"No existe el tratamiento {treatment_id}")
        return self._treatment_public(row)

    def end_treatment(self, treatment_id: str, end_date: str | None = None) -> dict[str, Any]:
        end = parse_date(end_date) if end_date else local_now(self._tz).date()
        cur = self._db.execute(
            "UPDATE treatments SET active=0, end_date=?, updated_at=? WHERE id=?",
            (end.isoformat(), iso(utcnow()), treatment_id),
        )
        if cur.rowcount != 1:
            raise ToolError(f"No existe el tratamiento {treatment_id}")
        return self.get_treatment(treatment_id)

    def doses_for_day(self, day: date) -> list[tuple[dict[str, Any], datetime]]:
        """Tomas programadas para un día: (tratamiento, hora local)."""
        out: list[tuple[dict[str, Any], datetime]] = []
        for t in self.list_treatments(active_only=True):
            if t["inicio"] > day.isoformat() or (t["fin"] and t["fin"] < day.isoformat()):
                continue
            for hhmm in t["horas"]:
                hour, minute = (int(x) for x in hhmm.split(":"))
                out.append(
                    (t, datetime(day.year, day.month, day.day, hour, minute, tzinfo=self._tz))
                )
        return sorted(out, key=lambda x: x[1])

    def ensure_intake(self, treatment_id: str, scheduled: datetime) -> str:
        intake_id = f"i-{treatment_id}-{scheduled.astimezone(self._tz):%Y%m%d%H%M}"
        self._db.execute(
            "INSERT OR IGNORE INTO med_intakes(id, treatment_id, scheduled_at, status) VALUES(?,?,?,?)",
            (intake_id, treatment_id, iso(scheduled), "pendiente"),
        )
        return intake_id

    def log_intake(
        self, intake_or_treatment_id: str, status: str, at: str | None = None
    ) -> dict[str, Any]:
        if status not in INTAKE_STATUSES[:2]:
            raise ToolError("Estado de toma: 'tomada' o 'saltada'")
        now = iso(utcnow())
        if intake_or_treatment_id.startswith("i-"):
            cur = self._db.execute(
                "UPDATE med_intakes SET status=?, recorded_at=? WHERE id=?",
                (status, now, intake_or_treatment_id),
            )
            if cur.rowcount != 1:
                raise ToolError("Toma no encontrada")
            return {"toma": intake_or_treatment_id, "estado": status}
        treatment = self.get_treatment(intake_or_treatment_id)
        when = parse_dt(at, self._tz) if at else local_now(self._tz)
        # Asocia a la toma programada más cercana del día.
        doses = [d for t, d in self.doses_for_day(when.date()) if t["id"] == treatment["id"]]
        scheduled = min(doses, key=lambda d: abs((d - when).total_seconds())) if doses else when
        intake_id = self.ensure_intake(treatment["id"], scheduled)
        self._db.execute(
            "UPDATE med_intakes SET status=?, recorded_at=? WHERE id=?", (status, now, intake_id)
        )
        return {"toma": intake_id, "estado": status, "programada": scheduled.isoformat()}

    def adherence(self, days: int = 30, treatment_id: str | None = None) -> dict[str, Any]:
        since = iso(utcnow() - timedelta(days=days))
        sql = "SELECT treatment_id, status, COUNT(*) AS n FROM med_intakes WHERE scheduled_at>=? AND scheduled_at<=?"
        params: list[Any] = [since, iso(utcnow())]
        if treatment_id:
            sql += " AND treatment_id=?"
            params.append(treatment_id)
        sql += " GROUP BY treatment_id, status"
        stats: dict[str, dict[str, int]] = {}
        for row in self._db.query(sql, params):
            stats.setdefault(row["treatment_id"], {})[row["status"]] = int(row["n"])
        result = {}
        for tid, counts in stats.items():
            total = sum(counts.values())
            taken = counts.get("tomada", 0)
            name = self.get_treatment(tid).get("medicamento", tid)
            result[tid] = {
                "medicamento": name,
                "tomadas": taken,
                "saltadas": counts.get("saltada", 0),
                "sin_registrar": counts.get("pendiente", 0),
                "adherencia": round(taken / total, 3) if total else None,
            }
        return {"dias": days, "tratamientos": result}

    # -- analíticas ------------------------------------------------------------------
    def add_lab_result(
        self, day: str, name: str, values: str, notes: str = "", doc_id: str = ""
    ) -> dict[str, Any]:
        d = parse_date(day)
        lab_id = new_id("lab")
        payload = {"nombre": name, "valores": values, "notas": notes, "doc_id": doc_id}
        self._db.execute(
            "INSERT INTO lab_results(id, created_at, day, payload) VALUES(?,?,?,?)",
            (lab_id, iso(utcnow()), d.isoformat(), self._db.enc(payload)),
        )
        return {"id": lab_id, "fecha": d.isoformat(), **payload}

    def list_lab_results(self, name: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._db.query("SELECT * FROM lab_results ORDER BY day DESC LIMIT ?", (limit * 3,))
        results = [{"id": r["id"], "fecha": r["day"], **self._db.dec(r["payload"])} for r in rows]
        if name:
            needle = name.lower()
            results = [r for r in results if needle in str(r["nombre"]).lower()]
        return results[:limit]

    def delete(self, kind: str, item_id: str) -> bool:
        tables = {
            "cita": "health_appointments",
            "tratamiento": "treatments",
            "analitica": "lab_results",
        }
        table = tables.get(kind)
        if table is None:
            raise ToolError(f"Tipo inválido. Usa uno de {tuple(tables)}")
        with self._db.transaction():
            if kind == "tratamiento":
                self._db.execute("DELETE FROM med_intakes WHERE treatment_id=?", (item_id,))
            cur = self._db.execute(f"DELETE FROM {table} WHERE id=?", (item_id,))
        return cur.rowcount == 1
