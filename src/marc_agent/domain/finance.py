"""Finanzas personales: SOLO registro interno y consulta.

No hay acceso bancario ni ninguna operación hacia fuera. Los datos entran a mano, por foto
de tique o por parseo de correos del banco/facturas.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from marc_agent.db import Database
from marc_agent.domain.recurrence import next_after, normalize_rule
from marc_agent.tools.base import ToolError
from marc_agent.util import iso, local_now, new_id, parse_date, utcnow

CATEGORIES = (
    "supermercado",
    "restaurantes",
    "transporte",
    "vivienda",
    "suministros",
    "salud",
    "ocio",
    "ropa",
    "hogar",
    "educacion",
    "viajes",
    "suscripciones",
    "regalos",
    "mascotas",
    "impuestos",
    "seguros",
    "prestamos",
    "otros",
)
DUE_KINDS = ("prestamo", "suscripcion", "factura", "impuesto", "seguro", "otro")
_PERIOD_RULES = {
    "mensual": "FREQ=MONTHLY",
    "anual": "FREQ=YEARLY",
    "semanal": "FREQ=WEEKLY",
    "trimestral": "FREQ=MONTHLY;INTERVAL=3",
}


def _amount(value: float) -> float:
    try:
        amount = round(float(value), 2)
    except (TypeError, ValueError) as exc:
        raise ToolError("Importe no numérico") from exc
    if not 0 < amount < 10_000_000:
        raise ToolError("Importe fuera de rango")
    return amount


def _category(value: str) -> str:
    cat = (value or "otros").strip().lower()
    return cat if cat in CATEGORIES else "otros"


class FinanceService:
    def __init__(self, db: Database, tz: ZoneInfo) -> None:
        self._db = db
        self._tz = tz

    def today(self) -> date:
        return local_now(self._tz).date()

    # -- gastos --------------------------------------------------------------------
    def add_expense(
        self,
        amount: float,
        category: str,
        day: str | None = None,
        merchant: str = "",
        note: str = "",
        currency: str = "EUR",
        source: str = "manual",
        doc_id: str = "",
    ) -> dict[str, Any]:
        d = parse_date(day) if day else self.today()
        expense_id = new_id("e")
        payload = {
            "importe": _amount(amount),
            "moneda": currency.upper()[:3],
            "categoria": _category(category),
            "comercio": merchant,
            "nota": note,
            "origen": source,
            "doc_id": doc_id,
        }
        self._db.execute(
            "INSERT INTO expenses(id, created_at, day, payload) VALUES(?,?,?,?)",
            (expense_id, iso(utcnow()), d.isoformat(), self._db.enc(payload)),
        )
        return {"id": expense_id, "fecha": d.isoformat(), **payload}

    def list_expenses(
        self, start: date, end: date, category: str | None = None
    ) -> list[dict[str, Any]]:
        rows = self._db.query(
            "SELECT * FROM expenses WHERE day>=? AND day<=? ORDER BY day",
            (start.isoformat(), end.isoformat()),
        )
        out = [{"id": r["id"], "fecha": r["day"], **self._db.dec(r["payload"])} for r in rows]
        if category:
            out = [e for e in out if e["categoria"] == _category(category)]
        return out

    # -- presupuesto ----------------------------------------------------------------
    def set_budget(
        self, month: str, total: float, per_category: dict[str, float] | None = None
    ) -> dict[str, Any]:
        datetime.strptime(month, "%Y-%m")
        payload = {
            "total": _amount(total),
            "por_categoria": {_category(k): _amount(v) for k, v in (per_category or {}).items()},
        }
        self._db.execute(
            "INSERT INTO budgets(month, payload, updated_at) VALUES(?,?,?) ON CONFLICT(month) "
            "DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
            (month, self._db.enc(payload), iso(utcnow())),
        )
        return {"mes": month, **payload}

    def get_budget(self, month: str) -> dict[str, Any] | None:
        row = self._db.query_one("SELECT payload FROM budgets WHERE month=?", (month,))
        return None if row is None else dict(self._db.dec(row["payload"]))

    def month_summary(self, month: str | None = None) -> dict[str, Any]:
        today = self.today()
        month = month or f"{today.year:04d}-{today.month:02d}"
        year, mon = (int(x) for x in month.split("-"))
        start = date(year, mon, 1)
        end = (date(year + (mon == 12), mon % 12 + 1, 1)) - timedelta(days=1)
        expenses = self.list_expenses(start, end)
        by_cat: dict[str, float] = defaultdict(float)
        for e in expenses:
            by_cat[e["categoria"]] += float(e["importe"])
        total = round(sum(by_cat.values()), 2)
        budget = self.get_budget(month)
        summary: dict[str, Any] = {
            "mes": month,
            "total_gastado": total,
            "num_gastos": len(expenses),
            "por_categoria": {
                k: round(v, 2) for k, v in sorted(by_cat.items(), key=lambda x: -x[1])
            },
            "suscripciones_mensuales_estimadas": round(self.monthly_subscription_cost(), 2),
        }
        if budget:
            summary["presupuesto"] = budget
            summary["restante"] = round(float(budget["total"]) - total, 2)
            over = {
                c: round(by_cat.get(c, 0) - float(limit), 2)
                for c, limit in budget.get("por_categoria", {}).items()
                if by_cat.get(c, 0) > float(limit)
            }
            if over:
                summary["categorias_excedidas"] = over
        return summary

    # -- vencimientos -----------------------------------------------------------------
    def add_due(
        self,
        title: str,
        due_date: str,
        amount: float | None = None,
        recurrence: str | None = None,
        kind: str = "otro",
        notes: str = "",
        ref_id: str | None = None,
    ) -> dict[str, Any]:
        if kind not in DUE_KINDS:
            raise ToolError(f"Tipo de vencimiento inválido. Usa uno de {DUE_KINDS}")
        rule = normalize_rule(recurrence)
        d = parse_date(due_date)
        due_id = new_id("d")
        payload: dict[str, Any] = {"titulo": title, "notas": notes, "moneda": "EUR"}
        if amount is not None:
            payload["importe"] = _amount(amount)
        now = iso(utcnow())
        self._db.execute(
            "INSERT INTO due_items(id, created_at, updated_at, due_date, recurrence, kind, ref_id, "
            "active, payload) VALUES(?,?,?,?,?,?,?,1,?)",
            (due_id, now, now, d.isoformat(), rule, kind, ref_id, self._db.enc(payload)),
        )
        return {"id": due_id, "vence": d.isoformat(), "recurrencia": rule, "tipo": kind, **payload}

    def get_due(self, due_id: str) -> dict[str, Any]:
        row = self._db.query_one("SELECT * FROM due_items WHERE id=?", (due_id,))
        if row is None:
            raise ToolError(f"No existe el vencimiento {due_id}")
        return self._due_public(row)

    def _due_public(self, row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "vence": row["due_date"],
            "recurrencia": row["recurrence"],
            "tipo": row["kind"],
            "activo": bool(row["active"]),
            **self._db.dec(row["payload"]),
        }

    def list_dues(self, days_ahead: int = 30, include_overdue: bool = True) -> list[dict[str, Any]]:
        today = self.today()
        limit = (today + timedelta(days=days_ahead)).isoformat()
        rows = self._db.query(
            "SELECT * FROM due_items WHERE active=1 AND due_date<=? ORDER BY due_date", (limit,)
        )
        dues = [self._due_public(r) for r in rows]
        if not include_overdue:
            dues = [d for d in dues if d["vence"] >= today.isoformat()]
        return dues

    def roll_dues(self) -> int:
        """Avanza vencimientos recurrentes pasados y desactiva los únicos ya vencidos."""
        today = self.today()
        rows = self._db.query(
            "SELECT * FROM due_items WHERE active=1 AND due_date<?", (today.isoformat(),)
        )
        changed = 0
        for row in rows:
            if row["recurrence"]:
                start = datetime.fromisoformat(row["due_date"]).replace(tzinfo=self._tz)
                yesterday = datetime(
                    today.year, today.month, today.day, tzinfo=self._tz
                ) - timedelta(seconds=1)
                nxt = next_after(row["recurrence"], start, yesterday)
                if nxt is None:
                    self._db.execute("UPDATE due_items SET active=0 WHERE id=?", (row["id"],))
                else:
                    self._db.execute(
                        "UPDATE due_items SET due_date=?, updated_at=? WHERE id=?",
                        (nxt.date().isoformat(), iso(utcnow()), row["id"]),
                    )
            elif row["due_date"] < (today - timedelta(days=3)).isoformat():
                self._db.execute("UPDATE due_items SET active=0 WHERE id=?", (row["id"],))
            else:
                continue
            changed += 1
        return changed

    # -- préstamos y suscripciones -----------------------------------------------------
    def add_loan(
        self,
        name: str,
        monthly_payment: float,
        payment_day: int,
        lender: str = "",
        start_date: str | None = None,
        end_date: str | None = None,
        outstanding: float | None = None,
        interest_rate: float | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        if not 1 <= payment_day <= 31:
            raise ToolError("Día de pago entre 1 y 31")
        loan_id = new_id("l")
        payload = {
            "nombre": name,
            "entidad": lender,
            "cuota_mensual": _amount(monthly_payment),
            "dia_pago": payment_day,
            "inicio": start_date or self.today().isoformat(),
            "fin": end_date or "",
            "pendiente": round(float(outstanding), 2) if outstanding is not None else None,
            "interes": interest_rate,
            "notas": notes,
        }
        now = iso(utcnow())
        self._db.execute(
            "INSERT INTO loans(id, created_at, updated_at, active, payload) VALUES(?,?,?,1,?)",
            (loan_id, now, now, self._db.enc(payload)),
        )
        rule = (
            f"FREQ=MONTHLY;BYMONTHDAY={min(payment_day, 28) if payment_day > 28 else payment_day}"
        )
        if end_date:
            rule += f";UNTIL={parse_date(end_date).strftime('%Y%m%d')}T235959"
        first = self._next_day_of_month(payment_day)
        due = self.add_due(
            f"Cuota préstamo {name}",
            first.isoformat(),
            monthly_payment,
            rule,
            "prestamo",
            ref_id=loan_id,
        )
        return {
            "id": loan_id,
            **payload,
            "vencimiento": due["id"],
            "proxima_cuota": first.isoformat(),
        }

    def _next_day_of_month(self, day: int) -> date:
        today = self.today()
        day = min(day, 28) if day > 28 else day
        candidate = date(today.year, today.month, day)
        if candidate < today:
            candidate = date(today.year + (today.month == 12), today.month % 12 + 1, day)
        return candidate

    def list_loans(self) -> list[dict[str, Any]]:
        rows = self._db.query("SELECT * FROM loans WHERE active=1 ORDER BY created_at")
        loans = []
        for r in rows:
            loan = {"id": r["id"], **self._db.dec(r["payload"])}
            due = self._db.query_one(
                "SELECT due_date FROM due_items WHERE ref_id=? AND active=1", (r["id"],)
            )
            loan["proxima_cuota"] = due["due_date"] if due else None
            loans.append(loan)
        return loans

    def add_subscription(
        self, name: str, amount: float, period: str, next_charge: str, notes: str = ""
    ) -> dict[str, Any]:
        rule = _PERIOD_RULES.get(period.lower())
        if rule is None:
            raise ToolError(f"Periodo inválido. Usa uno de {tuple(_PERIOD_RULES)}")
        sub_id = new_id("s")
        payload = {
            "nombre": name,
            "importe": _amount(amount),
            "periodo": period.lower(),
            "notas": notes,
        }
        now = iso(utcnow())
        self._db.execute(
            "INSERT INTO subscriptions(id, created_at, updated_at, active, payload) VALUES(?,?,?,1,?)",
            (sub_id, now, now, self._db.enc(payload)),
        )
        due = self.add_due(f"Cargo {name}", next_charge, amount, rule, "suscripcion", ref_id=sub_id)
        return {"id": sub_id, **payload, "proximo_cargo": due["vence"]}

    def list_subscriptions(self) -> list[dict[str, Any]]:
        rows = self._db.query("SELECT * FROM subscriptions WHERE active=1")
        out = []
        for r in rows:
            sub = {"id": r["id"], **self._db.dec(r["payload"])}
            due = self._db.query_one(
                "SELECT due_date FROM due_items WHERE ref_id=? AND active=1", (r["id"],)
            )
            sub["proximo_cargo"] = due["due_date"] if due else None
            out.append(sub)
        return out

    def monthly_subscription_cost(self) -> float:
        factors = {"mensual": 1.0, "anual": 1 / 12, "semanal": 52 / 12, "trimestral": 1 / 3}
        return sum(
            float(s["importe"]) * factors.get(str(s["periodo"]), 1.0)
            for s in self.list_subscriptions()
        )

    # -- borrado ---------------------------------------------------------------------
    def delete(self, kind: str, item_id: str) -> bool:
        tables = {
            "gasto": "expenses",
            "prestamo": "loans",
            "suscripcion": "subscriptions",
            "vencimiento": "due_items",
        }
        table = tables.get(kind)
        if table is None:
            raise ToolError(f"Tipo inválido. Usa uno de {tuple(tables)}")
        with self._db.transaction():
            if kind in ("prestamo", "suscripcion"):
                self._db.execute("UPDATE due_items SET active=0 WHERE ref_id=?", (item_id,))
                self._db.execute(
                    "DELETE FROM reminders WHERE due_id IN (SELECT id FROM due_items WHERE ref_id=?)",
                    (item_id,),
                )
            if kind == "vencimiento":
                self._db.execute("DELETE FROM reminders WHERE due_id=?", (item_id,))
            cur = self._db.execute(f"DELETE FROM {table} WHERE id=?", (item_id,))
        return cur.rowcount == 1
