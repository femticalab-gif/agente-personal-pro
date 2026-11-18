"""Proactividad: jobs programados con APScheduler (zona Europe/Madrid).

* Briefing diario (07:45), vigilancia de correo (cada 5 min), tick de recordatorios
  (cada minuto: recordatorios, medicación, citas, vencimientos, caducidades), resumen
  semanal (domingo), backup diario cifrado, heartbeat y cierre de sesiones inactivas.
* Si un job falla, se avisa por Telegram (como mucho una vez por hora y job).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from marc_agent.notify import Button
from marc_agent.ops.backup import BackupManager
from marc_agent.scheduler.briefing import BriefingBuilder
from marc_agent.services import Services
from marc_agent.util import fmt_local, iso, local_now, money, truncate, utcnow

log = logging.getLogger(__name__)

DAY_NAMES = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}


class JobRunner:
    """Lógica de cada job, independiente de APScheduler (testeable)."""

    def __init__(
        self,
        svc: Services,
        briefing: BriefingBuilder,
        backups: BackupManager,
        close_idle: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self.svc = svc
        self.briefing = briefing
        self.backups = backups
        self._close_idle = close_idle

    async def guarded(self, name: str, func: Callable[[], Awaitable[Any]]) -> bool:
        started = time.monotonic()
        try:
            await func()
        except Exception as exc:
            log.exception("job_failed", extra={"event": "job_failed", "job": name})
            hour = local_now(self.svc.config.tz).strftime("%Y%m%d%H")
            if self.svc.db.mark_once(f"jobfail:{name}:{hour}"):
                try:
                    await self.svc.notifier.send(
                        f"⚠️ El job programado «{name}» ha fallado ({type(exc).__name__}). "
                        "Lo reintentaré en la siguiente ejecución."
                    )
                except Exception:
                    log.warning("job_fail_notify_failed")
            return False
        self.svc.db.set_state(f"job_ok:{name}", iso(utcnow()))
        self.svc.audit.event("job_ok", job=name, ms=int((time.monotonic() - started) * 1000))
        return True

    # -- jobs -------------------------------------------------------------------------
    async def briefing_job(self) -> None:
        text, buttons = await self.briefing.daily()
        await self.svc.notifier.send(text, buttons)

    async def weekly_job(self) -> None:
        text, buttons = await self.briefing.weekly()
        await self.svc.notifier.send(text, buttons)

    async def mail_watch_job(self) -> None:
        if not self.svc.google_auth.has_token() or self.svc.state.kill_switch:
            return
        alerts = await self.svc.mail.check_new()
        for alert in alerts:
            if alert["categoria"] == "fraude":
                text = (
                    "🚨 Posible fraude/phishing\n"
                    f"De: {truncate(str(alert['de']), 80)}\nAsunto: {truncate(str(alert['asunto']), 120)}\n"
                    f"Motivos: {'; '.join(alert['fraude'])}\n"
                    "No responderé. Verifica por un canal oficial."
                )
            else:
                text = (
                    f"✉️ [{alert['categoria']}] {truncate(str(alert['de']), 60)}\n"
                    f"{truncate(str(alert['resumen'] or alert['asunto']), 300)}"
                )
            await self.svc.notifier.send(text)
        if alerts:
            self.svc.mail.mark_digested([a["id"] for a in alerts])

    async def backup_job(self) -> None:
        path = self.backups.create(self.svc.db)
        self.svc.db.set_state("last_backup", path.name)

    async def heartbeat_job(self) -> None:
        stamp = iso(utcnow())
        hb = self.svc.config.heartbeat_file
        hb.parent.mkdir(parents=True, exist_ok=True)
        hb.write_text(stamp, encoding="utf-8")
        self.svc.db.set_state("last_heartbeat", stamp)

    async def session_idle_job(self) -> None:
        if self._close_idle is not None:
            await self._close_idle()

    async def tick_job(self, now: datetime | None = None) -> int:
        """Recordatorios, medicación, citas, vencimientos y caducidades. Devuelve nº de avisos."""
        svc = self.svc
        if svc.state.kill_switch:
            return 0
        tz = svc.config.tz
        now = now or utcnow()
        local = now.astimezone(tz)
        sent = 0
        svc.finance.roll_dues()
        svc.reminders.refresh_linked()

        for reminder in svc.reminders.due_now(now):
            await svc.notifier.send(f"⏰ {reminder['texto']}")
            svc.reminders.mark_fired(reminder["id"], now)
            sent += 1

        full = svc.profile.prefs().get("detalle_salud_finanzas") == "completo"
        for treatment, scheduled in svc.health.doses_for_day(local.date()):
            if not (scheduled <= local < scheduled + timedelta(minutes=60)):
                continue
            intake_id = svc.health.ensure_intake(treatment["id"], scheduled)
            if svc.db.mark_once(f"med:{intake_id}"):
                await svc.notifier.send(
                    f"💊 Toma de las {scheduled:%H:%M}: {treatment['medicamento']} — {treatment['dosis']}",
                    [
                        [
                            Button("✅ Tomada", f"med:{intake_id}:t"),
                            Button("⏭ Saltada", f"med:{intake_id}:s"),
                        ]
                    ],
                )
                sent += 1

        horizon = max(svc.config.schedule.appointment_reminders_hours or [24])
        for appt in svc.health.list_appointments(now, now + timedelta(hours=horizon + 1)):
            at = datetime.fromisoformat(appt["cuando"])
            for hours in sorted(svc.config.schedule.appointment_reminders_hours):
                if at - timedelta(hours=hours) <= now < at:
                    if svc.db.mark_once(f"appt:{appt['id']}:{hours}"):
                        label = appt["especialidad"] if full else "cita médica"
                        place = f" en {appt['lugar']}" if appt.get("lugar") else ""
                        await svc.notifier.send(
                            f"🩺 Recordatorio: {label} {fmt_local(at, tz)}{place}."
                        )
                        sent += 1
                    break

        if local.time() >= svc.config.schedule.due_reminder_time:
            thresholds = sorted(svc.config.schedule.due_reminder_days)
            for due in svc.finance.list_dues(max(thresholds) + 1, include_overdue=False):
                days_left = (datetime.fromisoformat(due["vence"]).date() - local.date()).days
                reached = [d for d in thresholds if days_left <= d]
                if not reached:
                    continue
                keys = [f"due:{due['id']}:{due['vence']}:{d}" for d in reached]
                fresh = [svc.db.mark_once(k) for k in keys]
                if fresh[0]:
                    amount = (
                        f" — {money(float(due['importe']))}" if full and due.get("importe") else ""
                    )
                    when = (
                        "hoy"
                        if days_left == 0
                        else f"en {days_left} día{'s' if days_left != 1 else ''}"
                    )
                    await svc.notifier.send(
                        f"💶 Vence {when} ({due['vence']}): {due['titulo']}{amount}"
                    )
                    sent += 1

            doc_thresholds = sorted(svc.config.schedule.doc_expiry_days)
            for doc in svc.vault.expiring(local.date(), max(doc_thresholds)):
                days_left = (datetime.fromisoformat(doc["caduca"]).date() - local.date()).days
                reached = [d for d in doc_thresholds if days_left <= d]
                fresh = [
                    svc.db.mark_once(f"docexp:{doc['id']}:{doc['caduca']}:{d}") for d in reached
                ]
                if fresh and fresh[0]:
                    await svc.notifier.send(
                        f"📄 «{doc['titulo']}» caduca el {doc['caduca']} (en {days_left} días)."
                    )
                    sent += 1
        return sent


class AgentScheduler:
    def __init__(self, svc: Services, runner: JobRunner) -> None:
        self.svc = svc
        self.runner = runner
        self.scheduler = AsyncIOScheduler(timezone=svc.config.tz)

    def configure(self) -> None:
        sched = self.svc.config.schedule
        r = self.runner

        def add(name: str, func: Callable[[], Awaitable[Any]], trigger: str, **kw: Any) -> None:
            async def job() -> None:
                await r.guarded(name, func)

            self.scheduler.add_job(
                job,
                trigger,
                id=name,
                name=name,
                replace_existing=True,
                coalesce=True,
                max_instances=1,
                misfire_grace_time=300,
                **kw,
            )

        weekly_day = sched.weekly_day if sched.weekly_day in DAY_NAMES else "sun"
        add(
            "briefing",
            r.briefing_job,
            "cron",
            hour=sched.briefing_time.hour,
            minute=sched.briefing_time.minute,
        )
        add(
            "weekly",
            r.weekly_job,
            "cron",
            day_of_week=weekly_day,
            hour=sched.weekly_time.hour,
            minute=sched.weekly_time.minute,
        )
        add("mail_watch", r.mail_watch_job, "interval", minutes=sched.mail_watch_minutes)
        add("tick", r.tick_job, "interval", seconds=60)
        add(
            "backup",
            r.backup_job,
            "cron",
            hour=sched.backup_time.hour,
            minute=sched.backup_time.minute,
        )
        add("heartbeat", r.heartbeat_job, "interval", seconds=sched.heartbeat_seconds)
        add("session_idle", r.session_idle_job, "interval", minutes=5)

    def start(self) -> None:
        self.configure()
        self.scheduler.start()

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def next_runs(self) -> dict[str, str]:
        out = {}
        for job in self.scheduler.get_jobs():
            nrt = getattr(job, "next_run_time", None)
            out[job.id] = fmt_local(nrt, self.svc.config.tz) if nrt else "-"
        return out
