"""Gateway de Telegram: allowlist, rate limit, comandos, voz, fotos, export y borrado total."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from typing import Any

import httpx
import pytest
import respx

from marc_agent.agent.runtime import UserInput
from marc_agent.app import App
from marc_agent.gateway.api import Incoming, TelegramNotifier, split_text
from marc_agent.gateway.core import GatewayCore
from marc_agent.gateway.guard import AccessGuard, TokenBucket
from marc_agent.gateway.streaming import TelegramStreamSink, friendly_tool
from marc_agent.ops.export import decrypt_export
from marc_agent.services import Services
from tests.conftest import USER_ID
from tests.fakes import FakeAgentState, FakeLLM, FakeTelegramApi, Say, ToolCall

CHAT = USER_ID


def make_core(app: App, api: FakeTelegramApi, burst: int = 100) -> GatewayCore:
    guard = AccessGuard(USER_ID, per_minute=60, burst=burst, audit=app.svc.audit)
    return GatewayCore(
        app.svc,
        api,
        guard,
        app.runtime,
        app.gate,
        app.media,
        app.briefing,
        app.backups,
        export_key=app_secrets(app),
    )


def app_secrets(app: App) -> str:
    return str(app.svc.extras["export_key"])


@pytest.fixture
def api() -> FakeTelegramApi:
    return FakeTelegramApi()


@pytest.fixture
async def core(app: App, api: FakeTelegramApi, secrets: Any) -> AsyncIterator[GatewayCore]:
    app.svc.extras["export_key"] = secrets.export_key
    gateway = make_core(app, api)
    gateway.start()
    yield gateway
    await gateway.stop()


def msg(
    text: str = "", kind: str = "text", user: int = USER_ID, chat_type: str = "private", **kw: Any
) -> Incoming:
    command = ""
    args: list[str] = []
    if kind == "command":
        head, *args = text[1:].split()
        command = head
    return Incoming(
        update_id=1,
        user_id=user,
        chat_id=user,
        chat_type=chat_type,
        kind=kind,
        text=text,
        command=command,
        args=args,
        **kw,
    )


async def drain(core: GatewayCore) -> None:
    await asyncio.wait_for(core.queue.join(), timeout=5)
    for _ in range(5):
        await asyncio.sleep(0)


# -- allowlist y rate limit --------------------------------------------------------------
async def test_unauthorized_user_is_ignored_and_logged(
    core: GatewayCore,
    api: FakeTelegramApi,
    agent_state: FakeAgentState,
    caplog: Any,
) -> None:
    caplog.set_level(logging.WARNING)
    await core.handle(msg("hola", user=999))
    await core.handle(msg("/status", kind="command", user=999))
    await core.handle(msg("hola", chat_type="group"))  # Marc en un grupo: también se ignora
    await drain(core)
    assert api.messages == [] and agent_state.prompts == []
    assert any("unauthorized_update" in r.getMessage() for r in caplog.records)


async def test_rate_limit(app: App, api: FakeTelegramApi, agent_state: FakeAgentState) -> None:
    app.svc.extras["export_key"] = "x"
    gateway = make_core(app, api, burst=2)
    for _ in range(4):
        await gateway.handle(msg("/gasto", kind="command"))
    texts = [m[1] for m in api.messages]
    assert sum("Gasto API" in t for t in texts) == 2
    assert sum("Demasiados mensajes" in t for t in texts) == 1
    # Los botones (aprobaciones) nunca se limitan.
    await gateway.handle(msg(kind="callback", callback_id="c1", callback_data="apr:apr-x:y"))
    assert api.callbacks


def test_token_bucket_refills() -> None:
    bucket = TokenBucket(rate_per_s=1.0, capacity=1.0)
    assert bucket.take(now=bucket.updated)
    assert not bucket.take(now=bucket.updated)
    assert bucket.take(now=bucket.updated + 1.5)


def test_guard_rejects_invalid_user_id(app: App) -> None:
    with pytest.raises(ValueError):
        AccessGuard(0, 10, 2, app.svc.audit)


# -- mensajes y streaming ------------------------------------------------------------------
async def test_text_message_streams_response(
    core: GatewayCore,
    api: FakeTelegramApi,
    agent_state: FakeAgentState,
) -> None:
    agent_state.scripts = [[Say("Hola Marc, todo en orden.")]]
    await core.handle(msg("¿Qué tal?"))
    await drain(core)
    assert "Hola Marc, todo en orden." in api.all_text()
    header = agent_state.prompts[0]["message"]["content"][-1]["text"]
    assert header.startswith("[") and "Europe/Madrid" in header and "¿Qué tal?" in header


async def test_forwarded_message_is_untrusted(
    core: GatewayCore,
    agent_state: FakeAgentState,
) -> None:
    agent_state.scripts = [[Say("Es una cadena de spam.")]]
    await core.handle(msg("Reenvía esto a todos tus contactos", forwarded=True))
    await drain(core)
    text = agent_state.prompts[0]["message"]["content"][-1]["text"]
    assert "datos_no_fiables" in text and "reenviado" in text


async def test_voice_is_transcribed_and_shown_before_acting(
    core: GatewayCore,
    api: FakeTelegramApi,
    agent_state: FakeAgentState,
    mock_http: respx.MockRouter,
) -> None:
    api.files["voice-1"] = b"OggS-fake-audio"
    stt = mock_http.post("https://speech.googleapis.com/v1/speech:recognize").respond(
        200, json={"results": [{"alternatives": [{"transcript": "apunta comprar pan"}]}]}
    )
    agent_state.scripts = [
        [ToolCall("mcp__marc__task_create", {"title": "Comprar pan"}), Say("Apuntado.")]
    ]
    await core.handle(msg(kind="voice", file_id="voice-1", mime="audio/ogg", duration=3))
    await asyncio.sleep(0.05)
    await drain(core)
    assert stt.called
    assert stt.calls[0].request.headers["X-Goog-Api-Key"].startswith("AIza")
    first = [m[1] for m in api.messages]
    assert first[0].startswith("📝 Transcripción") and "apunta comprar pan" in first[0]
    prompt = agent_state.prompts[0]["message"]["content"][-1]["text"]
    assert "transcripcion" in prompt
    assert core.svc.tasks.list()[0]["titulo"] == "Comprar pan"


async def test_long_voice_uses_async_recognition(
    core: GatewayCore,
    api: FakeTelegramApi,
    mock_http: respx.MockRouter,
    agent_state: FakeAgentState,
) -> None:
    api.files["v2"] = b"OggS"
    core.svc.stt._poll = 0.0
    mock_http.post("https://speech.googleapis.com/v1/speech:longrunningrecognize").respond(
        200, json={"name": "op-1"}
    )
    mock_http.get("https://speech.googleapis.com/v1/operations/op-1").mock(
        side_effect=[
            httpx.Response(200, json={"done": False}),
            httpx.Response(
                200,
                json={
                    "done": True,
                    "response": {"results": [{"alternatives": [{"transcript": "nota larga"}]}]},
                },
            ),
        ]
    )
    await core.handle(msg(kind="voice", file_id="v2", mime="audio/ogg", duration=90))
    await asyncio.sleep(0.05)
    await drain(core)
    assert "nota larga" in api.messages[0][1]


async def test_voice_transcription_error(
    core: GatewayCore, api: FakeTelegramApi, mock_http: respx.MockRouter
) -> None:
    api.files["v3"] = b"OggS"
    mock_http.post("https://speech.googleapis.com/v1/speech:recognize").respond(500)
    await core.handle(msg(kind="voice", file_id="v3", mime="audio/ogg", duration=2))
    await asyncio.sleep(0.05)
    assert "Transcripción falló" in api.messages[0][1]


async def test_photo_receipt_extraction_and_expense_proposal(
    core: GatewayCore,
    api: FakeTelegramApi,
    llm: FakeLLM,
    svc: Services,
) -> None:
    api.files["photo-1"] = b"\xff\xd8\xff fake jpeg"
    llm.set(
        "vision_extract",
        {
            "tipo": "tique",
            "titulo": "Mercadona",
            "fecha": "2026-10-14",
            "caducidad": None,
            "emisor": "Mercadona",
            "importe_total": 23.4,
            "moneda": "EUR",
            "categoria_gasto": "supermercado",
            "fecha_vencimiento_pago": None,
            "cita_fecha_hora": None,
            "cita_especialidad": None,
            "resumen": "Compra semanal",
            "texto": "LECHE 1,20 PAN 0,90 TOTAL 23,40",
        },
    )
    await core.handle(msg(kind="photo", file_id="photo-1", mime="image/jpeg"))
    await asyncio.sleep(0.05)
    text, buttons = api.messages[-1][1], api.messages[-1][2]
    assert "Mercadona" in text and buttons is not None
    labels = [row[0].text for row in buttons]
    assert any("Registrar gasto" in label and "23,40" in label for label in labels)
    vision_call = llm.calls[-1]
    assert vision_call["content"][0]["type"] == "image"
    gasto = next(row[0].data for row in buttons if "gasto" in row[0].data)
    await core.handle(msg(kind="callback", callback_id="cb", callback_data=gasto))
    expenses = svc.finance.list_expenses(date(2026, 10, 1), date(2026, 10, 31))
    assert expenses[0]["importe"] == 23.4 and expenses[0]["categoria"] == "supermercado"
    assert "Gasto registrado" in api.all_text()
    # Repetir el botón no duplica.
    await core.handle(msg(kind="callback", callback_id="cb2", callback_data=gasto))
    assert "ya no está activa" in api.all_text()


async def test_pdf_document_with_caption_goes_to_agent(
    core: GatewayCore,
    api: FakeTelegramApi,
    llm: FakeLLM,
    agent_state: FakeAgentState,
    svc: Services,
) -> None:
    api.files["doc-1"] = b"%PDF-1.4 fake"
    llm.set(
        "vision_extract",
        {
            "tipo": "informe_medico",
            "titulo": "Informe cardiología",
            "fecha": "2026-10-01",
            "caducidad": None,
            "emisor": "Hospital",
            "importe_total": None,
            "moneda": None,
            "categoria_gasto": None,
            "fecha_vencimiento_pago": None,
            "cita_fecha_hora": "2026-11-03T09:30",
            "cita_especialidad": "cardiología",
            "resumen": "Revisión anual",
            "texto": "Paciente…",
        },
    )
    agent_state.scripts = [[Say("Resumen del informe…")]]
    await core.handle(
        msg(
            text="resúmemelo",
            kind="document",
            file_id="doc-1",
            mime="application/pdf",
            file_name="informe.pdf",
        )
    )
    await asyncio.sleep(0.05)
    await drain(core)
    buttons = next(m[2] for m in api.messages if m[2])
    assert any("cita" in row[0].data for row in buttons)
    cita = next(row[0].data for row in buttons if row[0].data.endswith(":cita"))
    await core.handle(msg(kind="callback", callback_id="c", callback_data=cita))
    # Rango explícito: la cita del informe tiene fecha fija y no debe depender del día de hoy.
    appts = svc.health.list_appointments(datetime(2026, 1, 1, tzinfo=UTC), None)
    assert appts and appts[0]["especialidad"] == "cardiología"
    content = agent_state.prompts[0]["message"]["content"]
    assert content[0]["type"] == "document"


async def test_unsupported_and_oversized_files(core: GatewayCore, api: FakeTelegramApi) -> None:
    await core.handle(msg(kind="document", file_id="z", mime="application/zip", file_name="a.zip"))
    await core.handle(
        msg(kind="document", file_id="z", mime="application/pdf", file_size=30_000_000)
    )
    await asyncio.sleep(0.05)
    texts = api.all_text()
    assert "no soportado" in texts and "demasiado grande" in texts


async def test_extraction_without_llm_keeps_pending_doc(
    core: GatewayCore,
    api: FakeTelegramApi,
    svc: Services,
) -> None:
    api.files["p"] = b"\xff\xd8"
    await core.handle(msg(kind="photo", file_id="p", mime="image/jpeg"))
    await asyncio.sleep(0.05)
    assert "pendiente" in api.messages[-1][1]
    assert svc.vault.list_docs()[0]["estado"] == "pendiente"


# -- comandos ---------------------------------------------------------------------------------
async def test_commands_basic(
    core: GatewayCore, api: FakeTelegramApi, svc: Services, agent_state: FakeAgentState
) -> None:
    svc.tasks.create("Renovar DNI", priority=1)
    svc.memory.save("Marc prefiere reuniones por la mañana", "usuario")
    for command in (
        "/help",
        "/status",
        "/gasto",
        "/pendientes",
        "/memoria ver",
        "/memoria buscar reuniones",
        "/memoria",
        "/dryrun on",
        "/dryrun",
        "/desconocido",
    ):
        await core.handle(msg(command, kind="command"))
    texts = api.all_text()
    assert "Comandos:" in texts and "📟 Estado" in texts and "Gasto API" in texts
    assert "Renovar DNI" in texts and "reuniones por la mañana" in texts
    assert svc.state.dry_run
    await core.handle(msg("/dryrun off", kind="command"))
    assert not svc.state.dry_run


async def test_start_triggers_onboarding(
    core: GatewayCore, api: FakeTelegramApi, agent_state: FakeAgentState
) -> None:
    agent_state.scripts = [[Say("Empecemos por tus préstamos.")]]
    await core.handle(msg("/start", kind="command"))
    await drain(core)
    assert "Hola, Marc" in api.all_text()
    assert "onboarding" in agent_state.prompts[0]["message"]["content"][-1]["text"]


async def test_stop_and_resume_commands(
    core: GatewayCore, api: FakeTelegramApi, svc: Services
) -> None:
    await core.handle(msg("/stop", kind="command"))
    assert svc.state.kill_switch and "ACTIVADO" in api.all_text()
    await core.handle(msg("/resume", kind="command"))
    assert not svc.state.kill_switch


async def test_memory_purge_commands(
    core: GatewayCore, api: FakeTelegramApi, svc: Services
) -> None:
    fact = svc.memory.save("Dato del correo", "email")
    svc.memory.save("Dato mío", "usuario")
    await core.handle(msg(f"/memoria purgar {fact.id}", kind="command"))
    assert svc.memory.count() == 1
    svc.memory.save("Otro externo", "web")
    await core.handle(msg("/memoria purgar externos", kind="command"))
    await core.handle(msg(kind="callback", callback_id="c", callback_data="mem:purge_ext"))
    assert [f.source for f in svc.memory.list_facts()] == ["usuario"]


async def test_briefing_command(
    core: GatewayCore, api: FakeTelegramApi, mock_http: respx.MockRouter
) -> None:
    mock_http.get("https://api.open-meteo.com/v1/forecast").respond(
        200,
        json={
            "daily": {
                "time": ["2026-10-15"],
                "weather_code": [1],
                "temperature_2m_max": [22.0],
                "temperature_2m_min": [14.0],
                "precipitation_probability_max": [10],
            }
        },
    )
    await core.handle(msg("/briefing", kind="command"))
    assert "Buenos días" in api.all_text() and "mayormente despejado" in api.all_text()


async def test_export_command_sends_encrypted_json(
    core: GatewayCore,
    api: FakeTelegramApi,
    svc: Services,
    secrets: Any,
) -> None:
    svc.finance.add_expense(12.5, "ocio", merchant="Cine")
    svc.vault.store(b"contenido", "text/plain", "nota.txt", title="Nota")
    await core.handle(msg("/export", kind="command"))
    _, data, name, _ = api.documents[0]
    assert name.endswith(".json.enc") and b"Cine" not in data
    dump = decrypt_export(data, secrets.export_key)
    assert dump["tablas"]["expenses"][0]["payload"]["comercio"] == "Cine"
    assert any(f.get("nombre") == "nota.txt" for f in dump["ficheros_vault"].values())


async def test_wipe_requires_double_confirmation(
    core: GatewayCore,
    api: FakeTelegramApi,
    svc: Services,
    app: App,
) -> None:
    svc.notes.create("Secreto", "texto")
    svc.vault.store(b"pdf", "application/pdf", "a.pdf", title="Doc")
    app.backups.create(svc.db)
    await core.handle(msg("/borrar_todo", kind="command"))
    button = api.messages[-1][2]
    assert button is not None
    # Texto sin pulsar el botón: no borra.
    await core.handle(msg("BORRAR TODO 123"))
    assert svc.notes.recent()
    nonce_data = button[0][0].data
    await core.handle(msg(kind="callback", callback_id="w", callback_data=nonce_data))
    code = api.messages[-1][1].split("BORRAR TODO ")[1].split("\n")[0]
    await core.handle(msg("BORRAR TODO XXXXXX"))  # código incorrecto
    assert svc.notes.recent()
    await core.handle(msg("/borrar_todo", kind="command"))
    await core.handle(
        msg(kind="callback", callback_id="w2", callback_data=api.messages[-1][2][0][0].data)
    )
    code = api.messages[-1][1].split("BORRAR TODO ")[1].split("\n")[0]
    await core.handle(msg(f"BORRAR TODO {code}"))
    assert svc.notes.recent() == []
    assert svc.vault.list_docs() == [] and list(svc.config.vault_dir.glob("*.bin")) == []
    assert app.backups.list_backups() == []
    assert "Borrado completado" in api.all_text()


async def test_wipe_button_expired(core: GatewayCore, api: FakeTelegramApi) -> None:
    await core.handle(msg(kind="callback", callback_id="w", callback_data="wipe:nope"))
    assert api.callbacks[-1][1] == "Confirmación caducada."


async def test_medication_button_logs_intake(
    core: GatewayCore, svc: Services, api: FakeTelegramApi
) -> None:
    t = svc.health.add_treatment("Ibuprofeno", "400 mg", ["08:00"])
    from datetime import datetime

    intake = svc.health.ensure_intake(t["id"], datetime(2026, 10, 15, 8, 0, tzinfo=svc.config.tz))
    await core.handle(
        msg(kind="callback", callback_id="m", callback_data=f"med:{intake}:t", message_id=55)
    )
    assert "tomada" in api.callbacks[-1][1]
    await core.handle(msg(kind="callback", callback_id="m2", callback_data="med:i-bad:s"))
    await core.handle(msg(kind="callback", callback_id="x", callback_data="desconocido"))
    assert api.callbacks[-1][1] == "Botón no reconocido"


async def test_queue_full_and_busy_notice(core: GatewayCore, api: FakeTelegramApi) -> None:
    core.queue = asyncio.Queue(maxsize=1)
    await core.queue.put((CHAT, UserInput("x")))
    await core.enqueue(CHAT, UserInput("y"))
    assert "demasiadas peticiones" in api.messages[-1][1]


# -- utilidades -----------------------------------------------------------------------------
def test_split_text_long() -> None:
    text = "linea\n" * 2000
    chunks = split_text(text, limit=4000)
    assert all(len(c) <= 4000 for c in chunks) and "".join(chunks) == text
    huge = "x" * 9000
    assert [len(c) for c in split_text(huge, limit=4000)] == [4000, 4000, 1000]


async def test_telegram_notifier_splits(api: FakeTelegramApi) -> None:
    notifier = TelegramNotifier(api, CHAT)
    from marc_agent.notify import Button

    mid = await notifier.send("a" * 5000, [[Button("ok", "x")]])
    assert len(api.messages) == 2 and api.messages[0][2] is None and api.messages[1][2] is not None
    assert mid == 2
    await notifier.edit(1, "editado")
    await notifier.send_document(b"d", "f.txt", "c" * 2000)
    assert api.edits and len(api.documents[0][3]) <= 1000


async def test_stream_sink_progress_and_split(api: FakeTelegramApi) -> None:
    sink = TelegramStreamSink(api, CHAT, edit_interval_s=0.0, progress_after_s=0.01)
    await sink.start()
    await sink.on_tool_use("mcp__marc__calendar_list")
    await asyncio.sleep(0.05)
    await sink.on_text_delta("Hola")
    await sink.finish("Respuesta final " + "x" * 4500)
    texts = api.all_text()
    assert "usando calendario" in texts
    assert any(m[1].startswith("Respuesta final") for m in api.messages) or any(
        e[2].startswith("Respuesta final") for e in api.edits
    )
    assert sink.closed
    assert friendly_tool("Agent") == "subagentes" and friendly_tool("mcp__marc__zzz") == "zzz"
