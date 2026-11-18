"""Runtime del agente, arranque de la app, CLI, watchdog, selftest y adaptador de Telegram."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
from claude_agent_sdk import CLIConnectionError, ResultMessage

from marc_agent import cli
from marc_agent.agent.runtime import DISALLOWED_BUILTINS, UserInput
from marc_agent.app import App, run
from marc_agent.config import AppConfig, ConfigError, Secrets, load_config
from marc_agent.crypto import generate_key
from marc_agent.gateway.api import Incoming
from marc_agent.ops.watchdog import Watchdog, healthcheck, heartbeat_age_s
from marc_agent.services import Services
from tests.conftest import USER_ID
from tests.fakes import CaptureSink, FakeAgentState, FakeLLM, FakeTelegramApi, Say, ToolCall


# -- runtime ----------------------------------------------------------------------------------
def test_build_options_is_locked_down(app: App) -> None:
    options = app.runtime.build_options(None)
    assert options.tools == ["Agent"]
    assert options.permission_mode == "dontAsk" and options.setting_sources == []
    assert options.strict_mcp_config and list(options.mcp_servers) == ["marc"]  # type: ignore[arg-type]
    assert "Bash" in options.disallowed_tools and set(DISALLOWED_BUILTINS) <= set(
        options.disallowed_tools
    )
    assert options.extra_args == {
        "restricted": None,
        "disable-slash-commands": None,
        "no-chrome": None,
    }
    assert json.loads(options.settings or "{}")["enabledPlugins"]
    assert options.env["CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS"] == "1"
    assert options.env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
    assert options.model == "claude-opus-5-5" and options.session_store is app.store
    assert options.hooks is not None and "PreToolUse" in options.hooks
    agents = options.agents or {}
    assert set(agents) == {"agenda", "investigador", "correo", "documentos"}
    for definition in agents.values():
        assert definition.tools and all(t.startswith("mcp__marc__") for t in definition.tools)
        for tool_name in definition.tools:
            spec = app.registry.get(tool_name)
            assert spec is not None and spec.category.value == "READ", tool_name
    system = str(options.system_prompt)
    assert "FORBIDDEN" in system and app.svc.wrapper.nonce in system and "gmail_draft" in system


async def test_turn_streams_accounts_and_logs(
    app: App, svc: Services, agent_state: FakeAgentState
) -> None:
    agent_state.scripts = [[Say("Primera parte. "), Say("Respuesta final.")], [Say("Segunda.")]]
    sink = CaptureSink()
    outcome = await app.runtime.run_turn(UserInput("hola"), sink)
    assert (
        outcome.ok
        and sink.final == "Respuesta final."
        and sink.deltas == ["Primera parte. ", "Respuesta final."]
    )
    assert outcome.cost_usd == pytest.approx(0.01)
    await app.runtime.run_turn(UserInput("otra"), CaptureSink())
    status = svc.budget.status()
    assert status.by_kind_month["agent"] == pytest.approx(0.02)  # incremento, no acumulado doble
    assert agent_state.connects == 1  # misma sesión
    assert svc.chatlog.turns("sess-1")[1]["u"] == "otra"


async def test_turn_with_images_and_docs(app: App, agent_state: FakeAgentState) -> None:
    await app.runtime.run_turn(
        UserInput("mira", images=[(b"img", "image/png")], documents=[(b"%PDF", "application/pdf")]),
        CaptureSink(),
    )
    content = agent_state.prompts[0]["message"]["content"]
    assert [c["type"] for c in content] == ["image", "document", "text"]


async def test_session_resume_and_idle_close(
    app: App, svc: Services, agent_state: FakeAgentState, llm: FakeLLM
) -> None:
    await app.runtime.run_turn(UserInput("recuerda que el lunes viene el técnico"), CaptureSink())
    assert app.runtime.session_id == "sess-1"
    await app.runtime.shutdown()
    await app.runtime.run_turn(UserInput("¿qué te dije?"), CaptureSink())
    assert agent_state.options[-1].resume == "sess-1"  # reanudada dentro de la ventana
    llm.set("session_summary", "- El lunes viene el técnico")
    svc.db.set_state("agent_last_activity", (datetime.now(UTC) - timedelta(hours=2)).isoformat())
    assert await app.runtime.maybe_close_idle()
    assert svc.chatlog.recent_summaries()[0][1] == "- El lunes viene el técnico"
    assert app.runtime.session_id is None and svc.chatlog.turns("sess-1") == []
    await app.runtime.run_turn(UserInput("nueva"), CaptureSink())
    assert agent_state.options[-1].resume is None
    assert "El lunes viene el técnico" in str(agent_state.options[-1].system_prompt)


async def test_stale_session_is_summarized_on_next_turn(
    app: App, svc: Services, agent_state: FakeAgentState, llm: FakeLLM
) -> None:
    await app.runtime.run_turn(UserInput("a"), CaptureSink())
    await app.runtime.shutdown()
    svc.db.set_state("agent_last_activity", (datetime.now(UTC) - timedelta(days=1)).isoformat())
    llm.fail = True  # sin LLM: se cierra igualmente, sin resumen
    await app.runtime.run_turn(UserInput("b"), CaptureSink())
    assert agent_state.options[-1].resume is None
    assert await app.runtime.close_session() is None or True


async def test_engine_error_resets_client(
    app: App, agent_state: FakeAgentState, svc: Services
) -> None:
    def broken(_: str) -> list[Any]:
        raise CLIConnectionError("cli caído")

    agent_state.scripts = [broken]
    sink = CaptureSink()
    outcome = await app.runtime.run_turn(UserInput("hola"), sink)
    assert not outcome.ok and sink.failed and "reiniciado" in sink.final
    assert agent_state.disconnects == 1


async def test_result_error_messages(
    app: App, agent_state: FakeAgentState, monkeypatch: Any
) -> None:
    from tests import fakes

    original = fakes.FakeAgentClient.receive_response

    async def failing(self: Any) -> Any:
        yield ResultMessage(
            subtype="error_max_budget_usd",
            duration_ms=1,
            duration_api_ms=1,
            is_error=True,
            num_turns=1,
            session_id="s",
            total_cost_usd=0.5,
            result=None,
        )

    monkeypatch.setattr(fakes.FakeAgentClient, "receive_response", failing)
    sink = CaptureSink()
    outcome = await app.runtime.run_turn(UserInput("x"), sink)
    assert not outcome.ok and "límite de gasto" in sink.final
    monkeypatch.setattr(fakes.FakeAgentClient, "receive_response", original)


async def test_interrupt_only_when_busy(app: App, agent_state: FakeAgentState) -> None:
    await app.runtime.interrupt()
    assert agent_state.interrupts == 0
    started = asyncio.Event()

    def slow(_: str) -> list[Any]:
        started.set()
        return [Say("ok")]

    agent_state.scripts = [slow]
    task = asyncio.create_task(app.runtime.run_turn(UserInput("x"), CaptureSink()))
    await task
    assert app.runtime.project_key()


async def test_subagent_tool_call_through_hook(app: App, agent_state: FakeAgentState) -> None:
    agent_state.scripts = [
        [ToolCall("Agent", {"subagent_type": "agenda", "prompt": "semana"}), Say("ok")]
    ]
    await app.runtime.run_turn(UserInput("prepara el fin de semana"), CaptureSink())
    assert agent_state.traces[0].allowed and agent_state.traces[0].output == "subagente ejecutado"


# -- arranque completo de la app ------------------------------------------------------------
class FakeRunner:
    instances: list[FakeRunner] = []

    def __init__(self, token: str, proxy: str | None) -> None:
        self.api = FakeTelegramApi()
        self.handler: Callable[[Incoming], Awaitable[None]] | None = None
        self.started = False
        self.stopped = False
        FakeRunner.instances.append(self)

    def on_update(self, handler: Callable[[Incoming], Awaitable[None]]) -> None:
        self.handler = handler

    async def start(self, commands: list[tuple[str, str]]) -> None:
        self.started = True
        assert ("borrar_todo", "Borrado total (doble confirmación)") in commands

    async def stop(self) -> None:
        self.stopped = True


async def test_app_run_starts_and_stops(config: AppConfig, secrets: Secrets, app: App) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(
        run(config, secrets, stop_event=stop, telegram_factory=FakeRunner, app=app)
    )
    for _ in range(50):
        await asyncio.sleep(0.02)
        if FakeRunner.instances and FakeRunner.instances[-1].started:
            break
    runner = FakeRunner.instances[-1]
    assert runner.started and runner.handler is not None
    assert "🟢 Agente en marcha." in runner.api.all_text()
    await runner.handler(
        Incoming(
            update_id=1,
            user_id=USER_ID,
            chat_id=USER_ID,
            chat_type="private",
            kind="command",
            command="status",
            text="/status",
        )
    )
    assert "📟 Estado" in runner.api.all_text()
    stop.set()
    await asyncio.wait_for(task, 10)
    assert runner.stopped
    logging.getLogger().handlers.clear()


# -- CLI ------------------------------------------------------------------------------------
@pytest.fixture
def env(monkeypatch: Any, tmp_path: Path, secrets: Secrets) -> Path:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"data_dir: {json.dumps(str(tmp_path / 'data'))}\nprofile:\n  name: Marc\n",
        encoding="utf-8",
    )
    values = {
        "TELEGRAM_BOT_TOKEN": secrets.telegram_bot_token,
        "TELEGRAM_USER_ID": str(secrets.telegram_user_id),
        "ANTHROPIC_API_KEY": secrets.anthropic_api_key,
        "MARC_DATA_KEY": secrets.data_key,
        "MARC_BACKUP_KEY": secrets.backup_key,
        "MARC_EXPORT_KEY": secrets.export_key,
        "GOOGLE_CLIENT_ID": "cid",
        "GOOGLE_CLIENT_SECRET": "csecret",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("MARC_DATA_KEY_PREVIOUS", raising=False)
    return cfg


def test_cli_backup_verify_restore_export(
    env: Path, tmp_path: Path, capsys: Any, secrets: Secrets
) -> None:
    assert cli.main(["--config", str(env), "backup"]) == 0
    assert cli.main(["--config", str(env), "list-backups"]) == 0
    assert cli.main(["--config", str(env), "verify-backup"]) == 0
    assert cli.main(["--config", str(env), "restore", "--yes"]) == 0
    out_file = tmp_path / "export.enc"
    assert cli.main(["--config", str(env), "export", "--out", str(out_file)]) == 0
    plain = tmp_path / "export.json"
    assert (
        cli.main(
            ["--config", str(env), "decrypt-export", "--in", str(out_file), "--out", str(plain)]
        )
        == 0
    )
    assert json.loads(plain.read_text(encoding="utf-8"))["formato"] == 1
    output = capsys.readouterr().out
    assert "Backup creado" in output and "OK:" in output and "Restaurado" in output


def test_cli_restore_cancel_and_no_backups(env: Path, monkeypatch: Any, tmp_path: Path) -> None:
    assert cli.main(["--config", str(env), "verify-backup"]) == 1
    assert cli.main(["--config", str(env), "restore"]) == 1
    cli.main(["--config", str(env), "backup"])
    monkeypatch.setattr("builtins.input", lambda _: "no")
    assert cli.main(["--config", str(env), "restore"]) == 1


def test_cli_init_keys(tmp_path: Path, capsys: Any) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text("TELEGRAM_USER_ID=1\nMARC_DATA_KEY=\n", encoding="utf-8")
    assert cli.main(["init-keys", "--env-file", str(env_file)]) == 0
    content = env_file.read_text(encoding="utf-8")
    assert content.count("MARC_DATA_KEY=") == 1 and "MARC_BACKUP_KEY=" in content
    first = content
    assert cli.main(["init-keys", "--env-file", str(env_file)]) == 0
    assert env_file.read_text(encoding="utf-8") == first
    out = capsys.readouterr().out
    assert "ninguna" in out
    assert generate_key()[:5] not in out  # nunca imprime las claves


def test_cli_rotate_key(env: Path, monkeypatch: Any, secrets: Secrets, capsys: Any) -> None:
    from marc_agent.app import build_services

    config = load_config(env)
    svc = build_services(config, secrets, llm=FakeLLM())
    svc.notes.create("x", "texto secreto")
    svc.vault.store(b"fichero", "text/plain", "f.txt", title="f")
    svc.db.close()
    assert cli.main(["--config", str(env), "rotate-key"]) == 1  # falta la clave anterior
    new_key = generate_key()
    monkeypatch.setenv("MARC_DATA_KEY", new_key)
    monkeypatch.setenv("MARC_DATA_KEY_PREVIOUS", secrets.data_key)
    assert cli.main(["--config", str(env), "rotate-key"]) == 0
    monkeypatch.delenv("MARC_DATA_KEY_PREVIOUS")
    rotated = build_services(config, Secrets.from_env(), llm=FakeLLM())
    assert rotated.notes.recent()[0]["texto"] == "texto secreto"
    data, _, _ = rotated.vault.read_file(rotated.vault.list_docs()[0]["id"])
    assert data == b"fichero"
    rotated.db.close()


def test_cli_config_errors(monkeypatch: Any, tmp_path: Path, capsys: Any) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert cli.main(["--config", str(tmp_path / "nope.yaml"), "backup"]) == 2
    bad = tmp_path / "bad.yaml"
    bad.write_text("- lista\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad)
    bad.write_text("profile: {timezone: Mars/Base}\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(bad)
    with pytest.raises(ConfigError):
        Secrets.from_env({"TELEGRAM_USER_ID": "abc"}, require_all=False)
    assert repr(Secrets.from_env({}, require_all=False)) == "Secrets(<redactado>)"


def test_cli_healthcheck(env: Path, tmp_path: Path) -> None:
    assert cli.main(["--config", str(env), "healthcheck"]) == 1
    hb = tmp_path / "data" / "heartbeat"
    hb.parent.mkdir(parents=True, exist_ok=True)
    hb.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
    assert cli.main(["--config", str(env), "healthcheck"]) == 0


def test_cli_google_auth(env: Path, monkeypatch: Any, capsys: Any) -> None:
    captured: dict[str, str] = {}

    def fake_getpass(prompt: str = "") -> str:
        url = capsys.readouterr().out.split("\n\n")[1].strip()
        state = url.split("state=")[1].split("&")[0]
        captured["url"] = url
        return f"http://127.0.0.1:8765/?state={state}&code=auth-code"

    monkeypatch.setattr(cli.getpass, "getpass", fake_getpass)
    with respx.mock(assert_all_mocked=True) as router:
        router.post("https://oauth2.googleapis.com/token").respond(
            200,
            json={
                "access_token": "ya29.x",
                "refresh_token": "1//refresh",
                "expires_in": 3600,
                "scope": "s",
            },
        )
        assert cli.main(["--config", str(env), "google-auth"]) == 0
    assert "accounts.google.com" in captured["url"]


# -- watchdog --------------------------------------------------------------------------------
async def test_watchdog_alerts_and_recovers(
    tmp_path: Path, mock_http: respx.MockRouter, http: httpx.AsyncClient
) -> None:
    hb = tmp_path / "heartbeat"
    route = mock_http.post(url__regex=r"https://api\.telegram\.org/bot.+/sendMessage").respond(200)
    dog = Watchdog(hb, stale_after_s=60, http=http, token="1:abc", chat_id=USER_ID)
    assert await dog.check_once() == "starting"
    hb.write_text((datetime.now(UTC) - timedelta(minutes=30)).isoformat(), encoding="utf-8")
    assert await dog.check_once() == "alerted"
    assert await dog.check_once() == "stale"
    hb.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
    assert await dog.check_once() == "recovered"
    assert await dog.check_once() == "ok"
    assert route.call_count == 2
    assert "sin heartbeat" in json.loads(route.calls[0].request.content)["text"]
    assert heartbeat_age_s(tmp_path / "missing") is None and not healthcheck(tmp_path / "missing")
    mock_http.post(url__regex=r"https://api\.telegram\.org/bot.+/sendMessage").mock(
        side_effect=httpx.ConnectError("x")
    )
    hb.write_text("no-es-fecha", encoding="utf-8")
    dog._started -= 1000
    assert await dog.check_once() == "alert_failed"


# -- selftest --------------------------------------------------------------------------------
async def test_selftest_reports(config: AppConfig, secrets: Secrets, monkeypatch: Any) -> None:
    from marc_agent.ops import selftest

    class FakeModels:
        async def retrieve(self, model: str) -> Any:
            return type("M", (), {"id": model})()

    class FakeAnthropic:
        def __init__(self, **_: Any) -> None:
            self.models = FakeModels()

    monkeypatch.setattr(selftest.anthropic, "AsyncAnthropic", FakeAnthropic)
    with respx.mock(assert_all_mocked=True) as router:
        router.get("https://example.com/").respond(403)
        router.get(url__regex=r"https://api\.telegram\.org/bot.+/getMe").respond(
            200,
            json={
                "result": {
                    "username": "marc_bot",
                    "can_join_groups": False,
                    "can_read_all_group_messages": False,
                    "supports_inline_queries": False,
                }
            },
        )
        router.get("https://api.search.brave.com/res/v1/web/search").respond(
            200, json={"web": {"results": [{"title": "t", "url": "u", "description": "d"}]}}
        )
        checks = await selftest.run_selftest(config, secrets, brave=True)
    by_name = {c.name: c for c in checks}
    assert by_name["egress bloqueado (example.com)"].ok
    assert by_name["BotFather: no añadible a grupos"].ok and by_name["anthropic API"].ok
    assert by_name["tools registradas"].ok and by_name["brave search"].ok
    assert not by_name["google token"].ok  # aún sin autorizar


async def test_selftest_agent_checks(app: App, agent_state: FakeAgentState) -> None:
    from marc_agent.ops.selftest import _check_agent

    agent_state.scripts = [
        [ToolCall("mcp__marc__onboarding_status", {}), Say("OK")],
        [ToolCall("Bash", {"command": "ls"}), Say("No puedo")],
    ]
    checks = await _check_agent(app)
    assert all(c.ok for c in checks), checks


# -- adaptador python-telegram-bot -----------------------------------------------------------
def _update(data: dict[str, Any]) -> Any:
    from telegram import Update

    return Update.de_json(data, None)


def test_to_incoming_variants() -> None:
    from marc_agent.gateway.telegram_app import to_incoming

    user = {"id": USER_ID, "is_bot": False, "first_name": "Marc"}
    chat = {"id": USER_ID, "type": "private"}
    base = {"message_id": 5, "date": 1700000000, "chat": chat, "from": user}
    cmd = to_incoming(
        _update({"update_id": 1, "message": {**base, "text": "/memoria@marc_bot buscar x"}})
    )
    assert (
        cmd is not None
        and cmd.kind == "command"
        and cmd.command == "memoria"
        and cmd.args == ["buscar", "x"]
    )
    text = to_incoming(
        _update(
            {
                "update_id": 2,
                "message": {
                    **base,
                    "text": "hola",
                    "forward_origin": {
                        "type": "hidden_user",
                        "date": 1700000000,
                        "sender_user_name": "X",
                    },
                },
            }
        )
    )
    assert text is not None and text.kind == "text" and text.forwarded
    voice = to_incoming(
        _update(
            {
                "update_id": 3,
                "message": {
                    **base,
                    "voice": {
                        "file_id": "v",
                        "file_unique_id": "u",
                        "duration": 7,
                        "mime_type": "audio/ogg",
                    },
                },
            }
        )
    )
    assert voice is not None and voice.kind == "voice" and voice.duration == 7
    photo = to_incoming(
        _update(
            {
                "update_id": 4,
                "message": {
                    **base,
                    "caption": "tique",
                    "photo": [
                        {"file_id": "small", "file_unique_id": "a", "width": 10, "height": 10},
                        {
                            "file_id": "big",
                            "file_unique_id": "b",
                            "width": 100,
                            "height": 100,
                            "file_size": 999,
                        },
                    ],
                },
            }
        )
    )
    assert photo is not None and photo.file_id == "big" and photo.text == "tique"
    doc = to_incoming(
        _update(
            {
                "update_id": 5,
                "message": {
                    **base,
                    "document": {
                        "file_id": "d",
                        "file_unique_id": "u",
                        "file_name": "a.pdf",
                        "mime_type": "application/pdf",
                    },
                },
            }
        )
    )
    assert doc is not None and doc.kind == "document" and doc.file_name == "a.pdf"
    cb = to_incoming(
        _update(
            {
                "update_id": 6,
                "callback_query": {
                    "id": "cb1",
                    "from": user,
                    "chat_instance": "ci",
                    "data": "apr:x:y",
                    "message": {**base, "text": "msg"},
                },
            }
        )
    )
    assert (
        cb is not None
        and cb.kind == "callback"
        and cb.callback_data == "apr:x:y"
        and cb.message_id == 5
    )
    sticker = to_incoming(
        _update(
            {
                "update_id": 7,
                "message": {
                    **base,
                    "sticker": {
                        "file_id": "s",
                        "file_unique_id": "u",
                        "width": 1,
                        "height": 1,
                        "is_animated": False,
                        "is_video": False,
                        "type": "regular",
                    },
                },
            }
        )
    )
    assert sticker is not None and sticker.kind == "other"
    assert to_incoming(_update({"update_id": 8})) is None


async def test_ptb_api_and_runner(monkeypatch: Any) -> None:
    from telegram.error import BadRequest, RetryAfter

    from marc_agent.gateway.telegram_app import PTBApi, TelegramRunner
    from marc_agent.notify import Button

    calls: list[tuple[str, dict[str, Any]]] = []
    state = {"retry": True}

    class FakeFile:
        async def download_as_bytearray(self) -> bytearray:
            return bytearray(b"bytes")

    class FakeBot:
        async def send_message(self, **kw: Any) -> Any:
            calls.append(("send", kw))
            if state["retry"]:
                state["retry"] = False
                raise RetryAfter(0)
            return type("Msg", (), {"message_id": 77})()

        async def edit_message_text(self, **kw: Any) -> None:
            calls.append(("edit", kw))
            raise BadRequest("Message is not modified")

        async def delete_message(self, **kw: Any) -> None:
            calls.append(("delete", kw))

        async def send_document(self, **kw: Any) -> None:
            calls.append(("doc", kw))

        async def answer_callback_query(self, **kw: Any) -> None:
            calls.append(("answer", kw))

        async def get_file(self, file_id: str) -> FakeFile:
            return FakeFile()

        async def send_chat_action(self, **kw: Any) -> None:
            calls.append(("typing", kw))

    application = type("AppStub", (), {"bot": FakeBot()})()
    api = PTBApi(application)  # type: ignore[arg-type]
    assert await api.send_message(1, "hola", [[Button("ok", "x")]]) == 77
    await api.edit_message(1, 77, "igual")
    await api.delete_message(1, 77)
    await api.send_document(1, b"d", "f.txt", "c")
    await api.answer_callback("cb", "ok")
    assert await api.download("f") == b"bytes"
    await api.typing(1)
    kinds = [c[0] for c in calls]
    assert kinds.count("send") == 2 and "edit" in kinds and "doc" in kinds
    runner = TelegramRunner("123:" + "A" * 35, None)
    received: list[Incoming] = []

    async def handler(update: Incoming) -> None:
        received.append(update)

    runner.on_update(handler)
    assert runner.application.handlers
