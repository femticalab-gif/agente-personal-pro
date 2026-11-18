"""Proactividad con mocks: briefing, vigilancia de correo, recordatorios y jobs."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import respx
import time_machine

from marc_agent.app import App
from marc_agent.scheduler.jobs import AgentScheduler
from marc_agent.services import Services
from tests.conftest import GmailMock, authorize_google, gmail_message
from tests.fakes import FakeLLM, FakeNotifier

CAL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
METEO = "https://api.open-meteo.com/v1/forecast"


def _weather(mock_http: respx.MockRouter) -> None:
    mock_http.get(METEO).respond(
        200,
        json={
            "daily": {
                "time": ["2026-10-15"],
                "weather_code": [61],
                "temperature_2m_max": [19.0],
                "temperature_2m_min": [12.0],
                "precipitation_probability_max": [70],
            }
        },
    )


@time_machine.travel(datetime(2026, 10, 15, 5, 45, tzinfo=UTC), tick=False)  # 07:45 Madrid
async def test_daily_briefing_with_all_sections(
    app: App,
    svc: Services,
    mock_http: respx.MockRouter,
    notifier: FakeNotifier,
) -> None:
    authorize_google(svc)
    _weather(mock_http)
    mock_http.get(CAL).respond(
        200,
        json={
            "items": [
                {
                    "id": "e1",
                    "summary": "Reunión colegio",
                    "start": {"dateTime": "2026-10-15T17:00:00+02:00"},
                    "end": {"dateTime": "2026-10-15T18:00:00+02:00"},
                    "location": "Escuela",
                },
                {
                    "id": "e2",
                    "summary": "Puente",
                    "start": {"date": "2026-10-15"},
                    "end": {"date": "2026-10-16"},
                },
            ]
        },
    )
    svc.finance.add_due("Seguro coche", "2026-10-20", 320.0, kind="seguro")
    svc.health.add_appointment("2026-10-16T09:30", "dermatología")
    svc.health.add_treatment("Omeprazol", "20 mg", ["08:00", "20:00"], start_date="2026-10-01")
    svc.tasks.create("Llamar al fontanero", due="2026-10-15T12:00", priority=1)
    svc.vault.store(b"dni", "image/jpeg", "dni.jpg", title="DNI", expires_at="2026-11-01")
    svc.db.execute(
        "INSERT INTO mail_seen(message_id, seen_at, important, digested, payload) VALUES(?,?,?,?,?)",
        (
            "m1",
            "2026-10-15T05:00:00+00:00",
            1,
            0,
            svc.db.enc(
                {
                    "de": "Colegio",
                    "asunto": "Excursión",
                    "resumen": "Excursión el viernes",
                    "categoria": "personal",
                    "fraude": [],
                }
            ),
        ),
    )
    svc.fraud_registry.flag("f1", "t1", "Banco Falso <x@y.z>", "Verifique su cuenta", ["frase"])
    await app.jobs.briefing_job()
    text = notifier.sent[-1].text
    for expected in (
        "Buenos días",
        "Reunión colegio",
        "todo el día",
        "Seguro coche",
        "cita médica",
        "2 tomas",
        "Llamar al fontanero",
        "lluvia ligera",
        "DNI",
        "Excursión el viernes",
        "Posibles fraudes",
    ):
        assert expected in text, expected
    # Minimización: sin importes ni nombres de medicamentos por defecto.
    assert "320" not in text and "Omeprazol" not in text and "dermatología" not in text
    assert svc.mail.pending_digest() == []
    svc.profile.set_pref("detalle_salud_finanzas", "completo")
    text_full, _ = await app.briefing.daily()
    assert "Omeprazol" in text_full and "320" in text_full


@time_machine.travel(datetime(2026, 10, 18, 17, 0, tzinfo=UTC), tick=False)  # domingo
async def test_sunday_briefing_and_weekly_include_memory_review(
    app: App,
    svc: Services,
    mock_http: respx.MockRouter,
    notifier: FakeNotifier,
) -> None:
    _weather(mock_http)
    svc.memory.save("El taller cierra los lunes", "web")
    svc.finance.add_expense(45.0, "restaurantes", day="2026-10-10")
    svc.finance.set_budget("2026-10", 40.0, {"restaurantes": 30.0})
    text, buttons = await app.briefing.daily()
    assert "Revisión semanal de memoria" in text and buttons is not None
    await app.jobs.weekly_job()
    weekly = notifier.sent[-1].text
    assert "Resumen semanal" in weekly and "restaurantes" in weekly and "excedidas" in weekly
    assert "El taller cierra los lunes" in weekly


async def test_mail_watch_alerts_only_important(
    app: App,
    svc: Services,
    mock_http: respx.MockRouter,
    notifier: FakeNotifier,
    llm: FakeLLM,
) -> None:
    authorize_google(svc)
    svc.db.set_state("mail_watch_initialized", "2026-01-01")

    def triage(kwargs: dict[str, Any]) -> dict[str, Any]:
        content = kwargs["content"]
        if "Factura" in content:
            return {"importance": "alta", "category": "factura", "summary": "Factura de la luz"}
        return {"importance": "baja", "category": "newsletter", "summary": "Boletín"}

    llm.set("mail_triage", triage)
    llm.set("fraud_check", {"requests_money_action": False, "reason": ""})
    GmailMock(
        mock_http,
        [
            gmail_message(
                "a",
                "Iberdrola <f@ib.example>",
                "Factura octubre",
                "Ya tienes tu factura disponible",
            ),
            gmail_message("b", "News <n@n.example>", "Boletín", "Novedades de la semana"),
        ],
    )
    await app.jobs.mail_watch_job()
    texts = notifier.texts()
    assert "Factura de la luz" in texts and "Boletín" not in texts
    pending = svc.mail.pending_digest()
    assert [m["id"] for m in pending] == ["b"]
    # Segunda pasada: nada nuevo.
    notifier.sent.clear()
    await app.jobs.mail_watch_job()
    assert notifier.sent == []


async def test_mail_watch_skips_without_google(app: App, notifier: FakeNotifier) -> None:
    await app.jobs.mail_watch_job()
    assert notifier.sent == []


async def test_reminders_tick(app: App, svc: Services, notifier: FakeNotifier) -> None:
    tz = svc.config.tz
    with time_machine.travel(datetime(2026, 10, 15, 8, 0, tzinfo=tz), tick=False):
        once = svc.reminders.create("Sacar la basura", at="2026-10-15T09:00")
        daily = svc.reminders.create(
            "Agua a las plantas", at="2026-10-15T09:00", recurrence="daily"
        )
        due = svc.finance.add_due(
            "Cuota coche", "2026-10-20", 250.0, recurrence="monthly", kind="prestamo"
        )
        linked = svc.reminders.create(
            "Revisa saldo para la cuota", due_id=due["id"], offset_days=-1, time_of_day="10:00"
        )
        assert linked["proximo"].startswith("2026-10-19T10:00")
    with time_machine.travel(datetime(2026, 10, 15, 9, 0, 30, tzinfo=tz), tick=False):
        await app.jobs.tick_job()
        texts = notifier.texts()
        assert "Sacar la basura" in texts and "Agua a las plantas" in texts
        assert not svc.reminders.get(once["id"])["activo"]
        assert svc.reminders.get(daily["id"])["proximo"].startswith("2026-10-16T09:00")
    with time_machine.travel(datetime(2026, 10, 19, 10, 0, 30, tzinfo=tz), tick=False):
        notifier.sent.clear()
        await app.jobs.tick_job()
        assert any("Revisa saldo" in s.text for s in notifier.sent)
    # El vencimiento avanza al mes siguiente y el recordatorio ligado se reprograma solo.
    with time_machine.travel(datetime(2026, 10, 21, 8, 0, tzinfo=tz), tick=False):
        await app.jobs.tick_job()
        assert svc.finance.get_due(due["id"])["vence"] == "2026-11-20"
        assert svc.reminders.get(linked["id"])["proximo"].startswith("2026-11-19T10:00")


async def test_medication_and_appointment_reminders(
    app: App,
    svc: Services,
    notifier: FakeNotifier,
) -> None:
    tz = svc.config.tz
    svc.health.add_treatment("Metformina", "850 mg", ["08:00", "20:00"], start_date="2026-10-01")
    svc.health.add_appointment("2026-10-16T10:00", "cardiología", place="Hospital Clínic")
    with time_machine.travel(datetime(2026, 10, 15, 8, 5, tzinfo=tz), tick=False):
        await app.jobs.tick_job()
        await app.jobs.tick_job()  # sin duplicados
    meds = [s for s in notifier.sent if "Toma" in s.text]
    assert len(meds) == 1 and meds[0].buttons is not None
    assert meds[0].buttons[0][0].data.startswith("med:i-")
    with time_machine.travel(datetime(2026, 10, 15, 10, 30, tzinfo=tz), tick=False):
        await app.jobs.tick_job()
    assert any(
        "Recordatorio: cita médica" in s.text and "Hospital Clínic" in s.text for s in notifier.sent
    )
    with time_machine.travel(datetime(2026, 10, 16, 8, 30, tzinfo=tz), tick=False):
        notifier.sent.clear()
        await app.jobs.tick_job()
        adherence = svc.health.adherence(30)  # ventana relativa al reloj fijado
    assert sum("Recordatorio" in s.text for s in notifier.sent) == 1  # el de 2 h antes
    assert adherence["tratamientos"]


async def test_due_and_document_expiry_reminders(
    app: App,
    svc: Services,
    notifier: FakeNotifier,
) -> None:
    tz = svc.config.tz
    svc.finance.add_due("IBI", "2026-10-22", 410.0, kind="impuesto")
    svc.vault.store(
        b"x", "application/pdf", "seguro.pdf", title="Póliza hogar", expires_at="2026-10-20"
    )
    with time_machine.travel(datetime(2026, 10, 15, 9, 0, tzinfo=tz), tick=False):
        await app.jobs.tick_job()
        await app.jobs.tick_job()
    assert sum("IBI" in s.text for s in notifier.sent) == 1
    assert sum("Póliza hogar" in s.text for s in notifier.sent) == 1
    assert "410" not in notifier.texts()
    with time_machine.travel(datetime(2026, 10, 21, 9, 0, tzinfo=tz), tick=False):
        notifier.sent.clear()
        await app.jobs.tick_job()
    assert any("Vence en 1 día" in s.text for s in notifier.sent)


async def test_tick_does_nothing_when_stopped(
    app: App, svc: Services, notifier: FakeNotifier
) -> None:
    svc.reminders.create("x", at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat())
    svc.state.kill_switch = True
    assert await app.jobs.tick_job(datetime.now(UTC) + timedelta(hours=1)) == 0


async def test_failed_job_notifies_once_per_hour(app: App, notifier: FakeNotifier) -> None:
    async def boom() -> None:
        raise RuntimeError("fallo")

    assert not await app.jobs.guarded("prueba", boom)
    assert not await app.jobs.guarded("prueba", boom)
    alerts = [s for s in notifier.sent if "prueba" in s.text]
    assert len(alerts) == 1 and "RuntimeError" in alerts[0].text

    async def ok() -> None:
        return None

    assert await app.jobs.guarded("otro", ok)
    assert app.svc.db.get_state("job_ok:otro")


async def test_backup_heartbeat_and_idle_jobs(app: App, svc: Services) -> None:
    await app.jobs.backup_job()
    assert svc.db.get_state("last_backup", "").startswith("backup-")
    await app.jobs.heartbeat_job()
    assert svc.config.heartbeat_file.read_text()
    await app.jobs.session_idle_job()


async def test_scheduler_configures_all_jobs(app: App, svc: Services) -> None:
    scheduler = AgentScheduler(svc, app.jobs)
    scheduler.configure()
    ids = {job.id for job in scheduler.scheduler.get_jobs()}
    assert ids == {
        "briefing",
        "weekly",
        "mail_watch",
        "tick",
        "backup",
        "heartbeat",
        "session_idle",
    }
    briefing = scheduler.scheduler.get_job("briefing")
    assert "hour='7'" in str(briefing.trigger) and "minute='45'" in str(briefing.trigger)
    assert str(briefing.trigger.timezone) == "Europe/Madrid"
    scheduler.start()
    assert scheduler.next_runs()
    scheduler.shutdown()


def test_roll_dues_deactivates_old_one_off(svc: Services) -> None:
    with time_machine.travel(date(2026, 10, 1), tick=False):
        svc.finance.add_due("Multa", "2026-10-02")
    with time_machine.travel(date(2026, 10, 10), tick=False):
        assert svc.finance.roll_dues() == 1
        assert svc.finance.list_dues(30) == []
