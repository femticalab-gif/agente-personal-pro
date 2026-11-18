"""Briefing diario y resumen semanal (plantillas deterministas: no gastan API)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from marc_agent.notify import Button, Keyboard
from marc_agent.services import Services
from marc_agent.tools.base import ToolError
from marc_agent.util import day_bounds, fmt_local, local_now, money, truncate

log = logging.getLogger(__name__)


class BriefingBuilder:
    def __init__(self, svc: Services) -> None:
        self.svc = svc

    @property
    def _full_detail(self) -> bool:
        return self.svc.profile.prefs().get("detalle_salud_finanzas") == "completo"

    async def daily(self) -> tuple[str, Keyboard | None]:
        svc = self.svc
        tz = svc.config.tz
        now = local_now(tz)
        today = now.date()
        sections: list[str] = svc.profile.prefs().get("briefing_secciones", [])
        days = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
        out = [f"☀️ Buenos días, {svc.config.profile.name}. {days[now.weekday()]} {now:%d/%m}."]
        buttons: Keyboard | None = None

        if "agenda" in sections:
            out.append(await self._agenda(today))
        if "salud" in sections:
            out.append(self._health(today))
        if "vencimientos" in sections:
            out.append(self._dues())
        if "correo" in sections:
            out.append(self._mail())
        if "tareas" in sections:
            out.append(self._tasks(today))
        if "clima" in sections and svc.config.weather_enabled:
            out.append(await self._weather())
        expiring = svc.vault.expiring(today, 30)
        if expiring:
            out.append(
                "📄 Documentos que caducan pronto:\n"
                + "\n".join(f"- {d['titulo']} ({d['caduca']})" for d in expiring[:5])
            )
        frauds = svc.fraud_registry.list_recent(5)
        recent_frauds = [
            f for f in frauds if f["created_at"] >= (now - timedelta(days=1)).isoformat()
        ]
        if recent_frauds:
            out.append(
                "🚨 Posibles fraudes detectados (no respondidos):\n"
                + "\n".join(
                    f"- {truncate(str(f.get('sender', '')), 60)}: {truncate(str(f.get('subject', '')), 80)}"
                    for f in recent_frauds
                )
            )
        if now.weekday() == 6:
            review, buttons = self._memory_review()
            if review:
                out.append(review)
        return "\n\n".join(s for s in out if s), buttons

    async def _agenda(self, today: Any) -> str:
        svc = self.svc
        if not svc.google_auth.has_token():
            return "📅 Agenda: Google no autorizado todavía."
        start, end = day_bounds(today, svc.config.tz)
        try:
            events = await svc.calendar.list(start, end)
        except ToolError as exc:
            return f"📅 Agenda: no disponible ({truncate(str(exc), 80)})."
        if not events:
            return "📅 Agenda: día libre."
        lines = [
            f"- {'todo el día' if e.all_day else fmt_local(e.start, svc.config.tz, with_date=False)} "
            f"{truncate(e.summary, 70)}{' @ ' + truncate(e.location, 40) if e.location else ''}"
            for e in events[:12]
        ]
        return "📅 Agenda de hoy:\n" + "\n".join(lines)

    def _health(self, today: Any) -> str:
        svc = self.svc
        tz = svc.config.tz
        start, _ = day_bounds(today, tz)
        appts = svc.health.list_appointments(start, start + timedelta(days=2))
        doses = svc.health.doses_for_day(today)
        lines = []
        for a in appts:
            when = fmt_local(datetime.fromisoformat(a["cuando"]), tz)
            label = a["especialidad"] if self._full_detail else "cita médica"
            lines.append(f"- {label}: {when}")
        if doses:
            hours = sorted({d.strftime("%H:%M") for _, d in doses})
            if self._full_detail:
                meds = sorted({str(t["medicamento"]) for t, _ in doses})
                lines.append(f"- Medicación: {', '.join(meds)} ({', '.join(hours)})")
            else:
                lines.append(f"- Medicación: {len(doses)} tomas ({', '.join(hours)})")
        return ("🩺 Salud:\n" + "\n".join(lines)) if lines else ""

    def _dues(self) -> str:
        dues = self.svc.finance.list_dues(7)
        if not dues:
            return "💶 Vencimientos 7 días: ninguno."
        lines = []
        for d in dues[:10]:
            amount = (
                f" — {money(float(d['importe']))}" if self._full_detail and d.get("importe") else ""
            )
            lines.append(f"- {d['vence']}: {truncate(str(d['titulo']), 60)}{amount}")
        return "💶 Vencimientos próximos 7 días:\n" + "\n".join(lines)

    def _mail(self) -> str:
        pending = self.svc.mail.pending_digest(40)
        if not pending:
            return "✉️ Correo: nada nuevo relevante."
        important = [m for m in pending if m["importante"]]
        rest = [m for m in pending if not m["importante"]]
        lines = [
            f"- [{m['categoria']}] {truncate(str(m['resumen'] or m['asunto']), 110)}"
            for m in important[:8]
        ]
        others = f"\n+ {len(rest)} correos sin importancia." if rest else ""
        self.svc.mail.mark_digested([m["id"] for m in pending])
        head = "✉️ Correo importante:\n" if lines else "✉️ Correo:"
        return head + "\n".join(lines) + others

    def _tasks(self, today: Any) -> str:
        svc = self.svc
        _, end = day_bounds(today, svc.config.tz)
        due = svc.tasks.list("abiertas", due_before=end)
        high = [t for t in svc.tasks.list("abiertas") if t["prioridad"] == 1 and t not in due]
        tasks = (due + high)[:10]
        reminders = [
            r
            for r in svc.reminders.list_active()
            if r["proximo"] and r["proximo"][:10] == today.isoformat()
        ]
        lines = [
            f"- {'⚠️ ' if t['vence'] and t['vence'] < local_now(svc.config.tz).isoformat() else ''}"
            f"{truncate(str(t['titulo']), 80)}"
            for t in tasks
        ]
        lines += [
            f"- ⏰ {r['proximo'][11:16]} {truncate(str(r['texto']), 70)}" for r in reminders[:6]
        ]
        return ("✅ Hoy:\n" + "\n".join(lines)) if lines else "✅ Sin tareas para hoy."

    async def _weather(self) -> str:
        try:
            days = await self.svc.weather.forecast(1)
        except ToolError:
            return ""
        if not days:
            return ""
        d = days[0]
        return f"🌦 {self.svc.config.profile.city}: {d.summary}, {d.t_min:.0f}–{d.t_max:.0f} °C, lluvia {d.rain_prob} %."

    def _memory_review(self) -> tuple[str, Keyboard | None]:
        facts = self.svc.memory.external_since(7)
        if not facts:
            return "", None
        lines = [f"- {f.id} [{f.source}] {truncate(f.text, 90)}" for f in facts[:15]]
        text = (
            "🧠 Revisión semanal de memoria: hechos de fuentes externas añadidos esta semana "
            "(no fiables). Borra los que sobren con /memoria purgar <id>:\n" + "\n".join(lines)
        )
        return text, [[Button("🧹 Purgar todos los externos", "mem:purge_ext")]]

    async def weekly(self) -> tuple[str, Keyboard | None]:
        svc = self.svc
        fin = svc.finance.month_summary()
        lines = [f"📊 Resumen semanal ({fin['mes']})"]
        top = list(fin["por_categoria"].items())[:5]
        lines.append(
            f"💶 Gastado este mes: {money(fin['total_gastado'])} en {fin['num_gastos']} gastos."
        )
        if top:
            lines.append("Top: " + ", ".join(f"{c} {money(v)}" for c, v in top))
        if "restante" in fin:
            lines.append(f"Presupuesto: quedan {money(fin['restante'])}.")
        if fin.get("categorias_excedidas"):
            lines.append("⚠️ Categorías excedidas: " + ", ".join(fin["categorias_excedidas"]))
        open_tasks = svc.tasks.list("abiertas")
        overdue = [
            t
            for t in open_tasks
            if t["vence"] and t["vence"] < local_now(svc.config.tz).isoformat()
        ]
        lines.append(f"✅ Pendientes: {len(open_tasks)} tareas abiertas ({len(overdue)} vencidas).")
        dues = svc.finance.list_dues(14)
        if dues:
            lines.append(
                "📅 Vencimientos 14 días: "
                + ", ".join(f"{truncate(str(d['titulo']), 40)} ({d['vence']})" for d in dues[:6])
            )
        adherence = svc.health.adherence(7)["tratamientos"]
        if adherence:
            parts = [
                f"{v['adherencia']:.0%}" for v in adherence.values() if v["adherencia"] is not None
            ]
            if parts:
                lines.append("💊 Adherencia 7 días: " + ", ".join(parts))
        review, buttons = self._memory_review()
        if review:
            lines.append(review)
        return "\n".join(lines), buttons
