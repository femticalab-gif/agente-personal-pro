"""Tools de calendario.

Crear/modificar/borrar eventos SIN invitados es WRITE_INTERNAL. CON invitados (actuales o
nuevos) es SENSITIVE: requiere aprobación y su contenido pasa por el clasificador
económico/salud (FORBIDDEN si habla de dinero o salud). Si no se puede comprobar si un
evento tiene invitados, se asume que sí (falla en cerrado).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from marc_agent.integrations.google.calendar import CalEvent, event_body, find_conflicts
from marc_agent.integrations.google.gmail import split_addresses
from marc_agent.permissions.categories import Category
from marc_agent.services import Services
from marc_agent.tools.base import ToolError, ToolOutput, ToolSpec, arr, b, obj_schema, s
from marc_agent.util import fmt_local, local_now, parse_dt


def build(svc: Services) -> list[ToolSpec]:
    cal = svc.calendar
    tz = svc.config.tz

    async def _existing(event_id: str) -> CalEvent | None:
        try:
            return await cal.get(event_id)
        except ToolError:
            return None

    def _new_attendees(args: dict[str, Any]) -> list[str] | None:
        if "attendees" not in args or args["attendees"] is None:
            return None
        return split_addresses(args["attendees"])

    async def classify_create(args: dict[str, Any]) -> Category:
        return Category.SENSITIVE if _new_attendees(args) else Category.WRITE_INTERNAL

    async def classify_existing(args: dict[str, Any]) -> Category:
        event = await _existing(args["event_id"])
        if event is None:
            return Category.SENSITIVE  # no verificable ⇒ se trata como con invitados
        new = _new_attendees(args)
        return Category.SENSITIVE if event.attendees or new else Category.WRITE_INTERNAL

    async def outbound_create(args: dict[str, Any]) -> str | None:
        if not _new_attendees(args):
            return None
        return "\n".join(str(args.get(k) or "") for k in ("summary", "description", "location"))

    async def outbound_update(args: dict[str, Any]) -> str | None:
        event = await _existing(args["event_id"])
        new = _new_attendees(args)
        has_guests = bool(new) or event is None or bool(event.attendees)
        if not has_guests:
            return None
        fields = []
        for key, current in (
            ("summary", event.summary if event else ""),
            ("description", event.description if event else ""),
            ("location", event.location if event else ""),
        ):
            fields.append(str(args.get(key) if args.get(key) is not None else current))
        return "\n".join(fields)

    def describe_create(args: dict[str, Any]) -> str:
        guests = args.get("attendees") or []
        extra = f" con invitados {', '.join(guests)} (recibirán invitación)" if guests else ""
        return f"Crear evento «{args.get('summary')}» el {args.get('start')}{extra}"

    def describe_update(args: dict[str, Any]) -> str:
        changes = {k: v for k, v in args.items() if k != "event_id"}
        return (
            f"Modificar evento {args.get('event_id')} (los invitados serán notificados): {changes}"
        )

    def describe_delete(args: dict[str, Any]) -> str:
        return (
            f"Borrar evento {args.get('event_id')} (se notificará la cancelación a los invitados)"
        )

    # -- handlers -----------------------------------------------------------------
    async def list_events(args: dict[str, Any]) -> ToolOutput:
        now = local_now(tz)
        start = (
            parse_dt(args["start"], tz)
            if args.get("start")
            else now.replace(hour=0, minute=0, second=0)
        )
        end = parse_dt(args["end"], tz) if args.get("end") else start + timedelta(days=7)
        events = await cal.list(start, end, args.get("query"))
        if not events:
            return ToolOutput("Sin eventos en ese rango.")
        lines = [
            f"- id={e.id} | {fmt_local(e.start, tz)}–{fmt_local(e.end, tz, with_date=False)}"
            f"{' (todo el día)' if e.all_day else ''} | {e.summary}"
            f"{' @ ' + e.location if e.location else ''}"
            f"{' | invitados: ' + ', '.join(e.attendees) if e.attendees else ''}"
            for e in events
        ]
        # Títulos y lugares pueden venir de invitaciones de terceros.
        wrapped = svc.wrapper.wrap("calendario", "\n".join(lines))
        return ToolOutput(
            wrapped.text, taint_source="calendario", injection_signals=wrapped.injection_signals
        )

    async def conflicts(args: dict[str, Any]) -> ToolOutput:
        start = parse_dt(args["start"], tz)
        end = parse_dt(args["end"], tz)
        if end <= start:
            raise ToolError("El fin debe ser posterior al inicio")
        events = await cal.list(start - timedelta(hours=12), end + timedelta(hours=12))
        clash = find_conflicts(events, start, end)
        if not clash:
            return ToolOutput("Hueco libre: sin conflictos.")
        return ToolOutput.data({"conflictos": [e.to_public() for e in clash]})

    async def create(args: dict[str, Any]) -> ToolOutput:
        start = parse_dt(args["start"], tz)
        all_day = bool(args.get("all_day"))
        end = (
            parse_dt(args["end"], tz)
            if args.get("end")
            else start + (timedelta(days=1) if all_day else timedelta(hours=1))
        )
        if end <= start:
            raise ToolError("El fin debe ser posterior al inicio")
        attendees = _new_attendees(args) or []
        body = event_body(
            args["summary"],
            start,
            end,
            tz,
            all_day=all_day,
            description=args.get("description"),
            location=args.get("location"),
            attendees=attendees or None,
        )
        clash = find_conflicts(
            await cal.list(start - timedelta(hours=12), end + timedelta(hours=12)), start, end
        )
        event = await cal.insert(body, notify_guests=bool(attendees))
        result: dict[str, Any] = {"creado": event.to_public()}
        if clash:
            result["aviso_conflictos"] = [e.summary for e in clash]
        return ToolOutput.data(result)

    async def update(args: dict[str, Any]) -> ToolOutput:
        event = await cal.get(args["event_id"])
        start = parse_dt(args["start"], tz) if args.get("start") else event.start
        end = (
            parse_dt(args["end"], tz)
            if args.get("end")
            else (start + (event.end - event.start) if args.get("start") else event.end)
        )
        body = event_body(
            args.get("summary") or event.summary,
            start,
            end,
            tz,
            all_day=event.all_day,
            description=args.get("description"),
            location=args.get("location"),
            attendees=_new_attendees(args),
        )
        new_attendees = _new_attendees(args)
        updated = await cal.patch(
            event.id, body, notify_guests=bool(event.attendees or new_attendees)
        )
        return ToolOutput.data({"actualizado": updated.to_public()})

    async def delete(args: dict[str, Any]) -> ToolOutput:
        event = await cal.get(args["event_id"])
        await cal.delete(event.id, notify_guests=bool(event.attendees))
        return ToolOutput(f"Evento «{event.summary}» borrado.")

    when = "Fecha/hora ISO 8601 (zona Europe/Madrid si no se indica)"
    event_props = {
        "summary": s("Título"),
        "start": s(when),
        "end": s(when + "; por defecto +1 h"),
        "all_day": b("Evento de día completo"),
        "description": s("Descripción"),
        "location": s("Lugar"),
        "attendees": arr("Emails de invitados (si hay, requiere aprobación)", maxItems=50),
    }
    return [
        ToolSpec(
            name="calendar_list",
            description="Lista eventos del calendario entre dos fechas (por defecto, próximos 7 días).",
            schema=obj_schema({"start": s(when), "end": s(when), "query": s("Texto a buscar")}),
            handler=list_events,
            category=Category.READ,
            read_only=True,
            subagent_ok=True,
        ),
        ToolSpec(
            name="calendar_conflicts",
            description="Comprueba si un hueco choca con eventos existentes.",
            schema=obj_schema({"start": s(when), "end": s(when)}, ["start", "end"]),
            handler=conflicts,
            category=Category.READ,
            read_only=True,
            subagent_ok=True,
        ),
        ToolSpec(
            name="calendar_create",
            description="Crea un evento. Sin invitados: directo. Con invitados: requiere aprobación "
            "y nunca puede tratar de dinero ni salud.",
            schema=obj_schema(event_props, ["summary", "start"]),
            handler=create,
            category=Category.WRITE_INTERNAL,
            classify=classify_create,
            outbound_text=outbound_create,
            describe=describe_create,
        ),
        ToolSpec(
            name="calendar_update",
            description="Modifica un evento. Si tiene o tendrá invitados requiere aprobación.",
            schema=obj_schema(
                {
                    "event_id": s("Id del evento"),
                    **{k: v for k, v in event_props.items() if k != "all_day"},
                },
                ["event_id"],
            ),
            handler=update,
            category=Category.WRITE_INTERNAL,
            classify=classify_existing,
            outbound_text=outbound_update,
            describe=describe_update,
        ),
        ToolSpec(
            name="calendar_delete",
            description="Borra un evento. Si tiene invitados requiere aprobación.",
            schema=obj_schema({"event_id": s("Id del evento")}, ["event_id"]),
            handler=delete,
            category=Category.WRITE_INTERNAL,
            classify=classify_existing,
            # La cancelación que reciben los invitados incluye título y descripción.
            outbound_text=outbound_update,
            describe=describe_delete,
        ),
    ]
