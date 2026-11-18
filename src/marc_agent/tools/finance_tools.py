"""Tools de finanzas personales: solo registro interno y consulta. Ninguna mueve dinero."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from marc_agent.domain.finance import CATEGORIES, DUE_KINDS
from marc_agent.permissions.categories import Category
from marc_agent.services import Services
from marc_agent.tools.base import ToolOutput, ToolSpec, i, n, obj_schema, s
from marc_agent.util import parse_date


def build(svc: Services) -> list[ToolSpec]:
    fin = svc.finance

    async def add_expense(a: dict[str, Any]) -> ToolOutput:
        return ToolOutput.data(
            {
                "registrado": fin.add_expense(
                    a["amount"],
                    a.get("category", "otros"),
                    a.get("date"),
                    a.get("merchant", ""),
                    a.get("note", ""),
                    a.get("currency", "EUR"),
                )
            }
        )

    async def list_expenses(a: dict[str, Any]) -> ToolOutput:
        end = parse_date(a["end"]) if a.get("end") else fin.today()
        start = parse_date(a["start"]) if a.get("start") else end - timedelta(days=30)
        items = fin.list_expenses(start, end, a.get("category"))
        total = round(sum(float(e["importe"]) for e in items), 2)
        return ToolOutput.data(
            {"desde": start.isoformat(), "hasta": end.isoformat(), "total": total, "gastos": items}
        )

    async def summary(a: dict[str, Any]) -> ToolOutput:
        return ToolOutput.data(fin.month_summary(a.get("month")))

    async def set_budget(a: dict[str, Any]) -> ToolOutput:
        month = a.get("month") or f"{fin.today():%Y-%m}"
        return ToolOutput.data(fin.set_budget(month, a["total"], a.get("per_category")))

    async def add_loan(a: dict[str, Any]) -> ToolOutput:
        return ToolOutput.data(
            fin.add_loan(
                a["name"],
                a["monthly_payment"],
                int(a["payment_day"]),
                a.get("lender", ""),
                a.get("start_date"),
                a.get("end_date"),
                a.get("outstanding"),
                a.get("interest_rate"),
                a.get("notes", ""),
            )
        )

    async def list_loans(_: dict[str, Any]) -> ToolOutput:
        return ToolOutput.data(fin.list_loans())

    async def add_subscription(a: dict[str, Any]) -> ToolOutput:
        return ToolOutput.data(
            fin.add_subscription(
                a["name"], a["amount"], a["period"], a["next_charge"], a.get("notes", "")
            )
        )

    async def list_subscriptions(_: dict[str, Any]) -> ToolOutput:
        subs = fin.list_subscriptions()
        return ToolOutput.data(
            {
                "suscripciones": subs,
                "coste_mensual_estimado": round(fin.monthly_subscription_cost(), 2),
            }
        )

    async def add_due(a: dict[str, Any]) -> ToolOutput:
        return ToolOutput.data(
            fin.add_due(
                a["title"],
                a["due_date"],
                a.get("amount"),
                a.get("recurrence"),
                a.get("kind", "otro"),
                a.get("notes", ""),
            )
        )

    async def list_dues(a: dict[str, Any]) -> ToolOutput:
        return ToolOutput.data(fin.list_dues(int(a.get("days_ahead", 30))))

    async def delete(a: dict[str, Any]) -> ToolOutput:
        ok = fin.delete(a["kind"], a["id"])
        return ToolOutput("Borrado." if ok else "No encontrado.", is_error=not ok)

    money = n("Importe positivo", exclusiveMinimum=0)
    return [
        ToolSpec(
            name="finance_add_expense",
            description="Registra un gasto en el registro interno (no realiza ningún pago).",
            schema=obj_schema(
                {
                    "amount": money,
                    "category": s("Categoría", enum=list(CATEGORIES)),
                    "date": s("YYYY-MM-DD (hoy por defecto)"),
                    "merchant": s("Comercio"),
                    "note": s("Nota"),
                    "currency": s("Moneda ISO (EUR)"),
                },
                ["amount", "category"],
            ),
            handler=add_expense,
            category=Category.WRITE_INTERNAL,
        ),
        ToolSpec(
            name="finance_list_expenses",
            description="Lista gastos registrados en un rango (por defecto últimos 30 días).",
            schema=obj_schema(
                {
                    "start": s("YYYY-MM-DD"),
                    "end": s("YYYY-MM-DD"),
                    "category": s("Categoría", enum=list(CATEGORIES)),
                }
            ),
            handler=list_expenses,
            category=Category.READ,
            read_only=True,
        ),
        ToolSpec(
            name="finance_summary",
            description="Resumen de un mes: gasto por categoría, presupuesto y suscripciones.",
            schema=obj_schema({"month": s("YYYY-MM (mes actual por defecto)")}),
            handler=summary,
            category=Category.READ,
            read_only=True,
        ),
        ToolSpec(
            name="finance_set_budget",
            description="Fija el presupuesto mensual (total y opcionalmente por categoría).",
            schema=obj_schema(
                {
                    "month": s("YYYY-MM"),
                    "total": money,
                    "per_category": {
                        "type": "object",
                        "additionalProperties": {"type": "number"},
                        "description": "Límite por categoría",
                    },
                },
                ["total"],
            ),
            handler=set_budget,
            category=Category.WRITE_INTERNAL,
        ),
        ToolSpec(
            name="finance_add_loan",
            description="Registra un préstamo y programa su cuota mensual como vencimiento.",
            schema=obj_schema(
                {
                    "name": s("Nombre (p. ej. hipoteca)"),
                    "monthly_payment": money,
                    "payment_day": i("Día de cobro 1-31", minimum=1, maximum=31),
                    "lender": s("Entidad"),
                    "start_date": s("YYYY-MM-DD"),
                    "end_date": s("YYYY-MM-DD"),
                    "outstanding": n("Capital pendiente"),
                    "interest_rate": n("TIN %"),
                    "notes": s("Notas"),
                },
                ["name", "monthly_payment", "payment_day"],
            ),
            handler=add_loan,
            category=Category.WRITE_INTERNAL,
        ),
        ToolSpec(
            name="finance_list_loans",
            description="Lista préstamos activos con su próxima cuota.",
            schema=obj_schema({}),
            handler=list_loans,
            category=Category.READ,
            read_only=True,
        ),
        ToolSpec(
            name="finance_add_subscription",
            description="Registra una suscripción y programa su próximo cargo.",
            schema=obj_schema(
                {
                    "name": s("Nombre"),
                    "amount": money,
                    "period": s("Periodo", enum=["mensual", "anual", "semanal", "trimestral"]),
                    "next_charge": s("Próximo cargo YYYY-MM-DD"),
                    "notes": s("Notas"),
                },
                ["name", "amount", "period", "next_charge"],
            ),
            handler=add_subscription,
            category=Category.WRITE_INTERNAL,
        ),
        ToolSpec(
            name="finance_list_subscriptions",
            description="Lista suscripciones activas y su coste mensual estimado.",
            schema=obj_schema({}),
            handler=list_subscriptions,
            category=Category.READ,
            read_only=True,
        ),
        ToolSpec(
            name="finance_add_due",
            description="Registra un vencimiento (factura, impuesto, seguro...) con recurrencia opcional.",
            schema=obj_schema(
                {
                    "title": s("Concepto"),
                    "due_date": s("YYYY-MM-DD"),
                    "amount": money,
                    "recurrence": s("RRULE (FREQ=MONTHLY;BYMONTHDAY=5) o monthly/yearly"),
                    "kind": s("Tipo", enum=list(DUE_KINDS)),
                    "notes": s("Notas"),
                },
                ["title", "due_date"],
            ),
            handler=add_due,
            category=Category.WRITE_INTERNAL,
        ),
        ToolSpec(
            name="finance_list_dues",
            description="Vencimientos próximos (incluye vencidos sin cerrar).",
            schema=obj_schema({"days_ahead": i("Días vista (30)", minimum=1, maximum=365)}),
            handler=list_dues,
            category=Category.READ,
            read_only=True,
            subagent_ok=True,
        ),
        ToolSpec(
            name="finance_delete",
            description="Borra un registro interno de finanzas (gasto, préstamo, suscripción o vencimiento).",
            schema=obj_schema(
                {
                    "kind": s("Tipo", enum=["gasto", "prestamo", "suscripcion", "vencimiento"]),
                    "id": s("Id"),
                },
                ["kind", "id"],
            ),
            handler=delete,
            category=Category.WRITE_INTERNAL,
        ),
    ]
