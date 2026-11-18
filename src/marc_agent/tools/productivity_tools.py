"""Tools de tareas, notas, recordatorios y contactos clave."""

from __future__ import annotations

from typing import Any

from marc_agent.domain.notes_tasks import TASK_STATUSES
from marc_agent.permissions.categories import Category
from marc_agent.services import Services
from marc_agent.tools.base import ToolOutput, ToolSpec, arr, i, obj_schema, s
from marc_agent.util import parse_dt


def build(svc: Services) -> list[ToolSpec]:
    tasks, notes, reminders, contacts = svc.tasks, svc.notes, svc.reminders, svc.contacts
    tz = svc.config.tz

    async def task_create(a: dict[str, Any]) -> ToolOutput:
        return ToolOutput.data(
            tasks.create(
                a["title"],
                a.get("due"),
                int(a.get("priority", 2)),
                a.get("project", ""),
                a.get("notes", ""),
            )
        )

    async def task_list(a: dict[str, Any]) -> ToolOutput:
        due_before = parse_dt(a["due_before"], tz) if a.get("due_before") else None
        return ToolOutput.data(
            tasks.list(a.get("status", "abiertas"), a.get("project"), due_before)
        )

    async def task_update(a: dict[str, Any]) -> ToolOutput:
        return ToolOutput.data(
            tasks.update(
                a["task_id"],
                title=a.get("title"),
                status=a.get("status"),
                due=a.get("due"),
                priority=a.get("priority"),
                project=a.get("project"),
                notes=a.get("notes"),
            )
        )

    async def task_delete(a: dict[str, Any]) -> ToolOutput:
        ok = tasks.delete(a["task_id"])
        return ToolOutput("Tarea borrada." if ok else "No encontrada.", is_error=not ok)

    async def note_create(a: dict[str, Any]) -> ToolOutput:
        return ToolOutput.data(
            notes.create(a["title"], a.get("body", ""), a.get("project", ""), a.get("tags"))
        )

    async def note_search(a: dict[str, Any]) -> ToolOutput:
        if a.get("query"):
            return ToolOutput.data(notes.search(a["query"], int(a.get("max_results", 10))))
        return ToolOutput.data(notes.recent(int(a.get("max_results", 10))))

    async def note_get(a: dict[str, Any]) -> ToolOutput:
        return ToolOutput.data(notes.get(a["note_id"]))

    async def note_update(a: dict[str, Any]) -> ToolOutput:
        return ToolOutput.data(
            notes.update(
                a["note_id"],
                titulo=a.get("title"),
                texto=a.get("body"),
                proyecto=a.get("project"),
                etiquetas=a.get("tags"),
            )
        )

    async def note_delete(a: dict[str, Any]) -> ToolOutput:
        ok = notes.delete(a["note_id"])
        return ToolOutput("Nota borrada." if ok else "No encontrada.", is_error=not ok)

    async def reminder_create(a: dict[str, Any]) -> ToolOutput:
        return ToolOutput.data(
            reminders.create(
                a["text"],
                a.get("at"),
                a.get("recurrence"),
                a.get("due_id"),
                int(a.get("offset_days", 0)),
                a.get("time_of_day", "09:00"),
            )
        )

    async def reminder_list(_: dict[str, Any]) -> ToolOutput:
        return ToolOutput.data(reminders.list_active())

    async def reminder_cancel(a: dict[str, Any]) -> ToolOutput:
        ok = reminders.cancel(a["reminder_id"])
        return ToolOutput("Recordatorio cancelado." if ok else "No encontrado.", is_error=not ok)

    async def contact_upsert(a: dict[str, Any]) -> ToolOutput:
        return ToolOutput.data(
            contacts.upsert(
                a["name"],
                a.get("relation", ""),
                a.get("email", ""),
                a.get("phone", ""),
                a.get("notes", ""),
                a.get("contact_id"),
            )
        )

    async def contact_search(a: dict[str, Any]) -> ToolOutput:
        if a.get("query"):
            return ToolOutput.data(contacts.search(a["query"]))
        return ToolOutput.data(contacts.all())

    async def contact_delete(a: dict[str, Any]) -> ToolOutput:
        ok = contacts.delete(a["contact_id"])
        return ToolOutput("Contacto borrado." if ok else "No encontrado.", is_error=not ok)

    return [
        ToolSpec(
            name="task_create",
            description="Crea una tarea con prioridad, fecha y proyecto.",
            schema=obj_schema(
                {
                    "title": s("Título"),
                    "due": s("Vence (ISO)"),
                    "priority": i("1 alta, 2 media, 3 baja", minimum=1, maximum=3),
                    "project": s("Proyecto"),
                    "notes": s("Notas"),
                },
                ["title"],
            ),
            handler=task_create,
            category=Category.WRITE_INTERNAL,
        ),
        ToolSpec(
            name="task_list",
            description="Lista tareas (abiertas por defecto).",
            schema=obj_schema(
                {
                    "status": s("Estado", enum=["abiertas", "todas", *TASK_STATUSES]),
                    "project": s("Proyecto"),
                    "due_before": s("Vencen antes de (ISO)"),
                }
            ),
            handler=task_list,
            category=Category.READ,
            read_only=True,
            subagent_ok=True,
        ),
        ToolSpec(
            name="task_update",
            description="Actualiza una tarea (estado, fecha, prioridad...).",
            schema=obj_schema(
                {
                    "task_id": s("Id (t-...)"),
                    "title": s("Título"),
                    "status": s("Estado", enum=list(TASK_STATUSES)),
                    "due": s("ISO"),
                    "priority": i("1-3", minimum=1, maximum=3),
                    "project": s("Proyecto"),
                    "notes": s("Notas"),
                },
                ["task_id"],
            ),
            handler=task_update,
            category=Category.WRITE_INTERNAL,
        ),
        ToolSpec(
            name="task_delete",
            description="Borra una tarea.",
            schema=obj_schema({"task_id": s("Id")}, ["task_id"]),
            handler=task_delete,
            category=Category.WRITE_INTERNAL,
        ),
        ToolSpec(
            name="note_create",
            description="Crea una nota.",
            schema=obj_schema(
                {
                    "title": s("Título"),
                    "body": s("Texto"),
                    "project": s("Proyecto"),
                    "tags": arr("Etiquetas"),
                },
                ["title"],
            ),
            handler=note_create,
            category=Category.WRITE_INTERNAL,
        ),
        ToolSpec(
            name="note_search",
            description="Busca notas por texto (sin consulta: las recientes).",
            schema=obj_schema(
                {"query": s("Texto"), "max_results": i("Máx.", minimum=1, maximum=50)}
            ),
            handler=note_search,
            category=Category.READ,
            read_only=True,
            subagent_ok=True,
        ),
        ToolSpec(
            name="note_get",
            description="Lee una nota completa.",
            schema=obj_schema({"note_id": s("Id (n-...)")}, ["note_id"]),
            handler=note_get,
            category=Category.READ,
            read_only=True,
            subagent_ok=True,
        ),
        ToolSpec(
            name="note_update",
            description="Modifica una nota.",
            schema=obj_schema(
                {
                    "note_id": s("Id"),
                    "title": s("Título"),
                    "body": s("Texto"),
                    "project": s("Proyecto"),
                    "tags": arr("Etiquetas"),
                },
                ["note_id"],
            ),
            handler=note_update,
            category=Category.WRITE_INTERNAL,
        ),
        ToolSpec(
            name="note_delete",
            description="Borra una nota.",
            schema=obj_schema({"note_id": s("Id")}, ["note_id"]),
            handler=note_delete,
            category=Category.WRITE_INTERNAL,
        ),
        ToolSpec(
            name="reminder_create",
            description="Crea un recordatorio: puntual ('at'), recurrente ('at' + 'recurrence' RRULE o "
            "daily/weekly/monthly) o ligado a un vencimiento ('due_id' + 'offset_days', p. ej. -1 = "
            "el día antes de la cuota).",
            schema=obj_schema(
                {
                    "text": s("Texto del aviso"),
                    "at": s("Fecha/hora ISO"),
                    "recurrence": s("RRULE o atajo"),
                    "due_id": s("Id de vencimiento (d-...)"),
                    "offset_days": i(
                        "Días respecto al vencimiento (-60..0)", minimum=-60, maximum=0
                    ),
                    "time_of_day": s("HH:MM para recordatorios ligados"),
                },
                ["text"],
            ),
            handler=reminder_create,
            category=Category.WRITE_INTERNAL,
        ),
        ToolSpec(
            name="reminder_list",
            description="Lista recordatorios activos.",
            schema=obj_schema({}),
            handler=reminder_list,
            category=Category.READ,
            read_only=True,
            subagent_ok=True,
        ),
        ToolSpec(
            name="reminder_cancel",
            description="Cancela un recordatorio.",
            schema=obj_schema({"reminder_id": s("Id (r-...)")}, ["reminder_id"]),
            handler=reminder_cancel,
            category=Category.WRITE_INTERNAL,
        ),
        ToolSpec(
            name="contact_upsert",
            description="Crea o actualiza un contacto clave (persona y relación, para contexto).",
            schema=obj_schema(
                {
                    "name": s("Nombre"),
                    "relation": s("Relación"),
                    "email": s("Email"),
                    "phone": s("Teléfono"),
                    "notes": s("Notas"),
                    "contact_id": s("Id para actualizar (c-...)"),
                },
                ["name"],
            ),
            handler=contact_upsert,
            category=Category.WRITE_INTERNAL,
        ),
        ToolSpec(
            name="contact_search",
            description="Busca contactos clave (sin consulta: todos).",
            schema=obj_schema({"query": s("Texto")}),
            handler=contact_search,
            category=Category.READ,
            read_only=True,
            subagent_ok=True,
        ),
        ToolSpec(
            name="contact_delete",
            description="Borra un contacto clave.",
            schema=obj_schema({"contact_id": s("Id")}, ["contact_id"]),
            handler=contact_delete,
            category=Category.WRITE_INTERNAL,
        ),
    ]
