"""Control de gasto de la API: aviso al 80 % y bloqueo al 100 % (diario y mensual)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from marc_agent.config import Budget, Pricing
from marc_agent.db import Database
from marc_agent.notify import Notifier
from marc_agent.util import iso, local_now, month_key

log = logging.getLogger(__name__)


class BudgetExceededError(RuntimeError):
    """Se alcanzó el límite de gasto diario o mensual."""


@dataclass(frozen=True)
class BudgetStatus:
    day_spent: float
    day_limit: float
    month_spent: float
    month_limit: float
    day_calls: int
    month_calls: int
    by_kind_month: dict[str, float]

    @property
    def blocked(self) -> bool:
        return self.day_spent >= self.day_limit or self.month_spent >= self.month_limit

    @property
    def remaining(self) -> float:
        return max(0.0, min(self.day_limit - self.day_spent, self.month_limit - self.month_spent))


class BudgetTracker:
    def __init__(
        self,
        db: Database,
        budget: Budget,
        pricing: Pricing,
        tz: ZoneInfo,
        notifier: Notifier | None = None,
    ) -> None:
        self._db = db
        self._budget = budget
        self._pricing = pricing
        self._tz = tz
        self.notifier = notifier

    def cost_for(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read: int = 0,
        cache_write: int = 0,
    ) -> float:
        # Modelo desconocido ⇒ se asume el precio más alto de la tabla (conservador).
        prices = self._pricing.table.get(model) or max(self._pricing.table.values())
        in_price, out_price = prices
        total = (
            input_tokens * in_price
            + output_tokens * out_price
            + cache_read * in_price * self._pricing.cache_read_factor
            + cache_write * in_price * self._pricing.cache_write_factor
        )
        return total / 1_000_000

    def _periods(self) -> tuple[str, str]:
        now = local_now(self._tz)
        return now.date().isoformat(), month_key(now)

    async def record(
        self,
        *,
        model: str,
        kind: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read: int = 0,
        cache_write: int = 0,
        cost_usd: float | None = None,
    ) -> float:
        cost = (
            cost_usd
            if cost_usd is not None
            else self.cost_for(model, input_tokens, output_tokens, cache_read, cache_write)
        )
        day, month = self._periods()
        now = local_now(self._tz)
        self._db.execute(
            "INSERT INTO api_usage(ts, day, month, model, kind, input_tokens, output_tokens, "
            "cost_usd) VALUES(?,?,?,?,?,?,?,?)",
            (iso(now), day, month, model, kind, input_tokens, output_tokens, float(cost)),
        )
        await self._check_thresholds()
        return cost

    def status(self) -> BudgetStatus:
        day, month = self._periods()
        d = self._db.query_one(
            "SELECT COALESCE(SUM(cost_usd),0) AS c, COUNT(*) AS n FROM api_usage WHERE day=?",
            (day,),
        )
        m = self._db.query_one(
            "SELECT COALESCE(SUM(cost_usd),0) AS c, COUNT(*) AS n FROM api_usage WHERE month=?",
            (month,),
        )
        kinds = self._db.query(
            "SELECT kind, SUM(cost_usd) AS c FROM api_usage WHERE month=? GROUP BY kind",
            (month,),
        )
        if d is None or m is None:  # pragma: no cover - SUM/COUNT siempre devuelve fila
            raise RuntimeError("Consulta de gasto sin resultado")
        return BudgetStatus(
            day_spent=float(d["c"]),
            day_limit=self._budget.daily_usd,
            month_spent=float(m["c"]),
            month_limit=self._budget.monthly_usd,
            day_calls=int(d["n"]),
            month_calls=int(m["n"]),
            by_kind_month={str(r["kind"]): float(r["c"]) for r in kinds},
        )

    def ensure_available(self) -> None:
        st = self.status()
        if st.blocked:
            raise BudgetExceededError(
                f"Límite de gasto alcanzado (hoy {st.day_spent:.2f}/{st.day_limit:.2f} USD, "
                f"mes {st.month_spent:.2f}/{st.month_limit:.2f} USD)"
            )

    async def _check_thresholds(self) -> None:
        st = self.status()
        day, month = self._periods()
        checks = (
            ("día", day, st.day_spent, st.day_limit),
            ("mes", month, st.month_spent, st.month_limit),
        )
        for label, period, spent, limit in checks:
            if limit <= 0:
                continue
            ratio = spent / limit
            if ratio >= 1.0 and self._db.mark_once(f"budget:block:{period}"):
                await self._notify(
                    f"⛔ Límite de gasto API del {label} alcanzado ({spent:.2f}/{limit:.2f} USD). "
                    "Bloqueo las llamadas al modelo hasta el siguiente periodo."
                )
            elif ratio >= self._budget.warn_ratio and self._db.mark_once(f"budget:warn:{period}"):
                await self._notify(
                    f"⚠️ Gasto API del {label} al {ratio:.0%} ({spent:.2f}/{limit:.2f} USD)."
                )

    async def _notify(self, text: str) -> None:
        log.warning("budget_threshold", extra={"event": "budget_threshold"})
        if self.notifier is not None:
            try:
                await self.notifier.send(text)
            except Exception:  # el aviso nunca debe romper la contabilidad
                log.exception("budget_notify_failed")
