"""Tools de salud (datos cifrados; nunca se comparten con terceros)."""

from __future__ import annotations

from typing import Any

from marc_agent.permissions.categories import Category
from marc_agent.services import Services
from marc_agent.tools.base import ToolOutput, ToolSpec, arr, i, obj_schema, s
from marc_agent.util import parse_dt


def build(svc: Services) -> list[ToolSpec]:
    health = svc.health
    tz = svc.config.tz

    async def add_appointment(a: dict[str, Any]) -> ToolOutput:
        return ToolOutput.data(
            health.add_appointment(
                a["when"],
                a["specialty"],
                a.get("doctor", ""),
                a.get("place", ""),
                a.get("notes", ""),
            )
        )

    async def list_appointments(a: dict[str, Any]) -> ToolOutput:
        start = parse_dt(a["start"], tz) if a.get("start") else None
        end = parse_dt(a["end"], tz) if a.get("end") else None
        return ToolOutput.data(health.list_appointments(start, end))

    async def cancel_appointment(a: dict[str, Any]) -> ToolOutput:
        ok = health.cancel_appointment(a["appointment_id"])
        return ToolOutput(
            "Cita cancelada en el registro." if ok else "No encontrada.", is_error=not ok
        )

    async def add_treatment(a: dict[str, Any]) -> ToolOutput:
        return ToolOutput.data(
            health.add_treatment(
                a["medication"],
                a["dose"],
                a["times"],
                a.get("start_date"),
                a.get("end_date"),
                a.get("notes", ""),
            )
        )

    async def list_treatments(a: dict[str, Any]) -> ToolOutput:
        return ToolOutput.data(
            health.list_treatments(active_only=not a.get("include_ended", False))
        )

    async def end_treatment(a: dict[str, Any]) -> ToolOutput:
        return ToolOutput.data(health.end_treatment(a["treatment_id"], a.get("end_date")))

    async def log_intake(a: dict[str, Any]) -> ToolOutput:
        return ToolOutput.data(health.log_intake(a["treatment_id"], a["status"], a.get("at")))

    async def adherence(a: dict[str, Any]) -> ToolOutput:
        return ToolOutput.data(health.adherence(int(a.get("days", 30)), a.get("treatment_id")))

    async def add_lab(a: dict[str, Any]) -> ToolOutput:
        return ToolOutput.data(
            health.add_lab_result(
                a["date"], a["name"], a["values"], a.get("notes", ""), a.get("doc_id", "")
            )
        )

    async def list_labs(a: dict[str, Any]) -> ToolOutput:
        return ToolOutput.data(health.list_lab_results(a.get("name"), int(a.get("limit", 20))))

    async def delete(a: dict[str, Any]) -> ToolOutput:
        ok = health.delete(a["kind"], a["id"])
        return ToolOutput("Borrado." if ok else "No encontrado.", is_error=not ok)

    return [
        ToolSpec(
            name="health_add_appointment",
            description="Registra una cita médica (recordatorios automáticos el día antes y 2 h antes).",
            schema=obj_schema(
                {
                    "when": s("Fecha/hora ISO"),
                    "specialty": s("Especialidad o motivo"),
                    "doctor": s("Profesional"),
                    "place": s("Lugar"),
                    "notes": s("Notas"),
                },
                ["when", "specialty"],
            ),
            handler=add_appointment,
            category=Category.WRITE_INTERNAL,
        ),
        ToolSpec(
            name="health_list_appointments",
            description="Lista citas médicas programadas (por defecto, próximas).",
            schema=obj_schema({"start": s("Desde ISO"), "end": s("Hasta ISO")}),
            handler=list_appointments,
            category=Category.READ,
            read_only=True,
            subagent_ok=True,
        ),
        ToolSpec(
            name="health_cancel_appointment",
            description="Marca una cita médica como cancelada en el registro interno.",
            schema=obj_schema({"appointment_id": s("Id (a-...)")}, ["appointment_id"]),
            handler=cancel_appointment,
            category=Category.WRITE_INTERNAL,
        ),
        ToolSpec(
            name="health_add_treatment",
            description="Registra un tratamiento con sus horas de toma (recordatorios y adherencia).",
            schema=obj_schema(
                {
                    "medication": s("Medicamento"),
                    "dose": s("Dosis"),
                    "times": arr("Horas HH:MM", minItems=1, maxItems=12),
                    "start_date": s("YYYY-MM-DD"),
                    "end_date": s("YYYY-MM-DD"),
                    "notes": s("Pauta / notas"),
                },
                ["medication", "dose", "times"],
            ),
            handler=add_treatment,
            category=Category.WRITE_INTERNAL,
        ),
        ToolSpec(
            name="health_list_treatments",
            description="Lista tratamientos (activos por defecto).",
            schema=obj_schema({"include_ended": {"type": "boolean"}}),
            handler=list_treatments,
            category=Category.READ,
            read_only=True,
        ),
        ToolSpec(
            name="health_end_treatment",
            description="Finaliza un tratamiento (deja de recordar las tomas).",
            schema=obj_schema(
                {"treatment_id": s("Id (m-...)"), "end_date": s("YYYY-MM-DD")}, ["treatment_id"]
            ),
            handler=end_treatment,
            category=Category.WRITE_INTERNAL,
        ),
        ToolSpec(
            name="health_log_intake",
            description="Registra una toma como tomada o saltada (adherencia).",
            schema=obj_schema(
                {
                    "treatment_id": s("Id de tratamiento (m-...) o de toma (i-...)"),
                    "status": s("Estado", enum=["tomada", "saltada"]),
                    "at": s("Hora ISO (ahora por defecto)"),
                },
                ["treatment_id", "status"],
            ),
            handler=log_intake,
            category=Category.WRITE_INTERNAL,
        ),
        ToolSpec(
            name="health_adherence",
            description="Adherencia a la medicación en los últimos N días.",
            schema=obj_schema(
                {"days": i("Días (30)", minimum=1, maximum=365), "treatment_id": s("Id")}
            ),
            handler=adherence,
            category=Category.READ,
            read_only=True,
        ),
        ToolSpec(
            name="health_add_lab_result",
            description="Registra una analítica o control con sus valores.",
            schema=obj_schema(
                {
                    "date": s("YYYY-MM-DD"),
                    "name": s("Prueba"),
                    "values": s("Valores"),
                    "notes": s("Notas"),
                    "doc_id": s("Documento del vault"),
                },
                ["date", "name", "values"],
            ),
            handler=add_lab,
            category=Category.WRITE_INTERNAL,
        ),
        ToolSpec(
            name="health_list_lab_results",
            description="Lista analíticas registradas (opcionalmente por nombre).",
            schema=obj_schema(
                {"name": s("Filtro por nombre"), "limit": i("Máx.", minimum=1, maximum=100)}
            ),
            handler=list_labs,
            category=Category.READ,
            read_only=True,
        ),
        ToolSpec(
            name="health_delete",
            description="Borra un registro interno de salud (cita, tratamiento o analítica).",
            schema=obj_schema(
                {"kind": s("Tipo", enum=["cita", "tratamiento", "analitica"]), "id": s("Id")},
                ["kind", "id"],
            ),
            handler=delete,
            category=Category.WRITE_INTERNAL,
        ),
    ]
