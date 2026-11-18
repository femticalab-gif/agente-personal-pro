"""Fixtures comunes. Todo externo está simulado: Telegram, Google, Anthropic, Brave, STT."""

from __future__ import annotations

import base64
import re
import time
from collections.abc import AsyncIterator, Iterator
from datetime import time as dtime
from typing import Any

import httpx
import pytest
import respx

from marc_agent.app import App, build_app, build_services
from marc_agent.config import AppConfig, Budget, Schedule, Secrets, Security, Telegram
from marc_agent.crypto import generate_key
from marc_agent.services import Services
from tests.fakes import FakeAgentClient, FakeAgentState, FakeLLM, FakeNotifier

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CAL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
DRIVE = "https://www.googleapis.com/drive/v3/files"

USER_ID = 4242


@pytest.fixture
def secrets() -> Secrets:
    return Secrets(
        telegram_bot_token="123456789:" + "A" * 35,
        telegram_user_id=USER_ID,
        anthropic_api_key="sk-ant-api03-testkey-0123456789",
        data_key=generate_key(),
        backup_key=generate_key(),
        export_key=generate_key(),
        google_client_id="client-id.apps.googleusercontent.com",
        google_client_secret="GOCSPX-test-secret-value",
        brave_api_key="brave-test-key-123456",
        google_stt_api_key="AIza" + "S" * 35,
    )


@pytest.fixture
def config(tmp_path: Any) -> AppConfig:
    return AppConfig(
        data_dir=tmp_path / "data",
        security=Security(approval_timeout_s=2),
        budget=Budget(daily_usd=5.0, monthly_usd=60.0),
        schedule=Schedule(due_reminder_time=dtime(0, 0)),
        telegram=Telegram(stream_edit_interval_s=0.0, progress_after_s=30.0),
        weather_enabled=True,
    )


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def mock_http() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


@pytest.fixture
async def http(mock_http: respx.MockRouter) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def svc(
    config: AppConfig,
    secrets: Secrets,
    llm: FakeLLM,
    http: httpx.AsyncClient,
    notifier: FakeNotifier,
) -> Iterator[Services]:
    services = build_services(config, secrets, http=http, llm=llm)
    services.notifier.target = notifier
    yield services
    services.db.close()


@pytest.fixture
def agent_state() -> FakeAgentState:
    return FakeAgentState()


@pytest.fixture
def app(config: AppConfig, secrets: Secrets, svc: Services, agent_state: FakeAgentState) -> App:
    holder: dict[str, App] = {}

    async def runner(full_name: str, args: dict[str, Any]) -> tuple[str, bool]:
        application = holder["app"]
        spec = application.registry.resolve_external(full_name)
        if spec is None:
            return "Tool not found", True
        out = await application.executor.run(spec, args)
        return out.text, out.is_error

    def factory(options: Any) -> FakeAgentClient:
        return FakeAgentClient(options, agent_state, runner)

    application = build_app(config, secrets, svc=svc, client_factory=factory)
    holder["app"] = application
    return application


def authorize_google(svc: Services) -> None:
    svc.db.execute(
        "INSERT OR REPLACE INTO oauth_tokens(provider, payload, updated_at) VALUES(?,?,?)",
        (
            "google",
            svc.db.enc(
                {
                    "access_token": "ya29.test-access-token",
                    "refresh_token": "1//test-refresh-token-abcdefghijkl",
                    "expires_at": time.time() + 3600,
                    "scope": " ".join(
                        [
                            "https://www.googleapis.com/auth/gmail.modify",
                            "https://www.googleapis.com/auth/calendar.events",
                            "https://www.googleapis.com/auth/drive.readonly",
                        ]
                    ),
                }
            ),
            "2026-01-01T00:00:00+00:00",
        ),
    )
    svc.google_auth._cached = None


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def gmail_message(
    message_id: str,
    sender: str,
    subject: str,
    body: str = "",
    html: str | None = None,
    thread_id: str = "thread-1",
) -> dict[str, Any]:
    parts = []
    if body:
        parts.append({"mimeType": "text/plain", "body": {"data": _b64(body)}})
    if html is not None:
        parts.append({"mimeType": "text/html", "body": {"data": _b64(html)}})
    return {
        "id": message_id,
        "threadId": thread_id,
        "snippet": (body or subject)[:80],
        "labelIds": ["INBOX", "UNREAD"],
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "To", "value": "marc@example.com"},
                {"name": "Subject", "value": subject},
                {"name": "Date", "value": "Wed, 14 Oct 2026 09:00:00 +0200"},
                {"name": "Message-ID", "value": f"<{message_id}@mail.example.com>"},
            ],
            "parts": parts,
        },
    }


class GmailMock:
    """Gmail simulado con respx: búsqueda, lectura, borradores, envío, etiquetas."""

    def __init__(self, router: respx.MockRouter, messages: list[dict[str, Any]]) -> None:
        self.messages = {m["id"]: m for m in messages}
        self.drafts: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.modified: list[tuple[str, dict[str, Any]]] = []
        self.labels: dict[str, str] = {"INBOX": "INBOX"}
        router.get(f"{GMAIL}/messages").mock(side_effect=self._list)
        router.get(url__regex=rf"^{re.escape(GMAIL)}/messages/[^/?]+(\?.*)?$").mock(
            side_effect=self._get
        )
        router.post(f"{GMAIL}/drafts").mock(side_effect=self._draft)
        router.post(f"{GMAIL}/messages/send").mock(side_effect=self._send)
        router.post(url__regex=rf"^{re.escape(GMAIL)}/messages/[^/]+/modify$").mock(
            side_effect=self._modify
        )
        router.get(f"{GMAIL}/labels").mock(side_effect=self._labels)
        router.post(f"{GMAIL}/labels").mock(side_effect=self._create_label)

    def _list(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": [{"id": i} for i in self.messages]})

    def _get(self, request: httpx.Request) -> httpx.Response:
        message_id = request.url.path.rsplit("/", 1)[-1]
        if message_id not in self.messages:
            return httpx.Response(404, json={"error": {"message": "Not Found"}})
        return httpx.Response(200, json=self.messages[message_id])

    def _draft(self, request: httpx.Request) -> httpx.Response:
        import json

        self.drafts.append(json.loads(request.content))
        return httpx.Response(200, json={"id": f"draft-{len(self.drafts)}"})

    def _send(self, request: httpx.Request) -> httpx.Response:
        import json

        self.sent.append(json.loads(request.content))
        return httpx.Response(200, json={"id": f"sent-{len(self.sent)}"})

    def _modify(self, request: httpx.Request) -> httpx.Response:
        import json

        self.modified.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={})

    def _labels(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"labels": [{"name": k, "id": v} for k, v in self.labels.items()]}
        )

    def _create_label(self, request: httpx.Request) -> httpx.Response:
        import json

        name = json.loads(request.content)["name"]
        self.labels[name] = f"Label_{len(self.labels)}"
        return httpx.Response(200, json={"id": self.labels[name], "name": name})


def decode_raw(raw: str) -> str:
    padded = raw + "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


async def approve_next(notifier: FakeNotifier, app: App, approve: bool = True) -> None:
    """Configura el notificador para pulsar el botón de la próxima aprobación."""

    async def on_send(msg: Any) -> None:
        if msg.buttons and msg.buttons[0][0].data.startswith("apr:"):
            approval_id = msg.buttons[0][0].data.split(":")[1]
            import asyncio

            async def press() -> None:
                await asyncio.sleep(0)
                await app.gate.approvals.resolve(approval_id, approve)

            asyncio.get_running_loop().create_task(press())

    notifier.on_send = on_send
