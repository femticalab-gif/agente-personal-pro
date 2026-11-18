"""Autodiagnóstico en el servidor (``marc-agent selftest``).

Comprueba cifrado, egress bloqueado, Telegram (y la configuración de BotFather vía getMe),
Anthropic, Google (token y scopes), búsqueda y, opcionalmente, el cableado real del agente
(hook PreToolUse activo, herramientas propias y ausencia de shell).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import anthropic
import httpx

from marc_agent.app import App, build_app
from marc_agent.config import AppConfig, Secrets
from marc_agent.integrations.google.auth import SCOPES
from marc_agent.tools.base import ToolError


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class _CaptureSink:
    text: str = ""
    tools: list[str] = field(default_factory=list)

    async def on_text_delta(self, text: str) -> None:
        return None

    async def on_tool_use(self, tool_name: str) -> None:
        self.tools.append(tool_name)

    async def finish(self, text: str) -> None:
        self.text = text

    async def fail(self, text: str) -> None:
        self.text = text


async def _check_egress(http: httpx.AsyncClient) -> Check:
    try:
        response = await http.get("https://example.com/", timeout=10)
    except httpx.HTTPError as exc:
        return Check("egress bloqueado (example.com)", True, type(exc).__name__)
    blocked = response.status_code in (403, 407)
    return Check("egress bloqueado (example.com)", blocked, f"HTTP {response.status_code}")


async def _check_telegram(http: httpx.AsyncClient, secrets: Secrets) -> list[Check]:
    try:
        response = await http.get(f"https://api.telegram.org/bot{secrets.telegram_bot_token}/getMe")
    except httpx.HTTPError as exc:
        return [Check("telegram getMe", False, type(exc).__name__)]
    if response.status_code != 200:
        return [Check("telegram getMe", False, f"HTTP {response.status_code}")]
    me: dict[str, Any] = response.json().get("result", {})
    return [
        Check("telegram getMe", True, f"@{me.get('username')}"),
        Check(
            "BotFather: no añadible a grupos",
            me.get("can_join_groups") is False,
            f"can_join_groups={me.get('can_join_groups')}",
        ),
        Check(
            "BotFather: privacy mode",
            me.get("can_read_all_group_messages") is False,
            f"can_read_all_group_messages={me.get('can_read_all_group_messages')}",
        ),
        Check(
            "BotFather: inline desactivado",
            me.get("supports_inline_queries") is False,
            f"supports_inline_queries={me.get('supports_inline_queries')}",
        ),
    ]


async def _check_agent(app: App) -> list[Check]:
    checks: list[Check] = []
    calls: list[str] = []
    allowed_calls: list[str] = []
    original = app.gate.decide

    async def spy(tool_name: str, args: dict[str, Any], agent_id: str | None = None) -> Any:
        calls.append(tool_name)
        result = await original(tool_name, args, agent_id)
        if result[0]:
            allowed_calls.append(tool_name)
        return result

    app.gate.decide = spy  # type: ignore[method-assign]
    from marc_agent.agent.runtime import UserInput

    sink = _CaptureSink()
    await app.runtime.run_turn(
        UserInput(
            "SELFTEST: llama una vez a la herramienta onboarding_status y responde solo 'OK'."
        ),
        sink,
    )
    checks.append(
        Check(
            "agente: hook PreToolUse activo",
            any("onboarding_status" in c for c in calls),
            ", ".join(calls) or "sin llamadas",
        )
    )
    sink2 = _CaptureSink()
    calls.clear()
    allowed_calls.clear()
    await app.runtime.run_turn(
        UserInput("SELFTEST: intenta ejecutar el comando de shell 'ls' con la herramienta Bash."),
        sink2,
    )
    # Correcto si el modelo ni siquiera ve la shell o si el hook la deniega.
    shell_tools = {"Bash", "BashOutput", "PowerShell"}
    executed_shell = any(c in shell_tools for c in allowed_calls)
    checks.append(
        Check(
            "agente: sin shell disponible",
            not executed_shell,
            f"intentos={[t for t in sink2.tools if t in shell_tools]} permitidos={allowed_calls}",
        )
    )
    await app.runtime.close_session("selftest")
    return checks


async def run_selftest(
    config: AppConfig, secrets: Secrets, *, agent: bool = False, brave: bool = False
) -> list[Check]:
    app = build_app(config, secrets)
    svc = app.svc
    http = svc.extras["http"]
    if not isinstance(http, httpx.AsyncClient):
        raise TypeError("cliente HTTP no inicializado")
    checks: list[Check] = []
    token = svc.cipher.encrypt_str("selftest")
    checks.append(Check("cifrado Fernet", svc.cipher.decrypt_str(token) == "selftest"))
    checks.append(Check("SQLite integrity_check", svc.db.integrity_ok()))
    checks.append(Check("tools registradas", len(app.registry) > 40, f"{len(app.registry)} tools"))
    checks.append(await _check_egress(http))
    checks.extend(await _check_telegram(http, secrets))
    try:
        client = anthropic.AsyncAnthropic(
            api_key=secrets.anthropic_api_key,
            http_client=anthropic.DefaultAsyncHttpxClient(proxy=config.network.effective_proxy)
            if config.network.effective_proxy
            else None,
        )
        model = await client.models.retrieve(config.models.main)
        checks.append(Check("anthropic API", True, model.id))
    except anthropic.APIError as exc:
        checks.append(Check("anthropic API", False, type(exc).__name__))
    if svc.google_auth.has_token():
        try:
            await svc.google_auth.access_token(force_refresh=True)
            granted = set(svc.google_auth.granted_scopes())
            missing = [s for s in SCOPES if s not in granted]
            extra = [s for s in granted if s not in SCOPES and "openid" not in s]
            checks.append(Check("google token", True))
            checks.append(
                Check(
                    "google scopes mínimos",
                    not missing and not extra,
                    f"faltan={missing} sobran={extra}",
                )
            )
        except ToolError as exc:
            checks.append(Check("google token", False, str(exc)))
    else:
        checks.append(Check("google token", False, "no autorizado (marc-agent google-auth)"))
    if brave:
        try:
            results = await svc.search.search("previsión tiempo", 1)
            checks.append(Check("brave search", bool(results)))
        except ToolError as exc:
            checks.append(Check("brave search", False, str(exc)))
    if agent:
        checks.extend(await _check_agent(app))
    await http.aclose()
    svc.db.close()
    return checks
