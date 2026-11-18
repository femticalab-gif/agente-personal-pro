"""Puntos de control: hook ``PreToolUse`` del Agent SDK y guardia del ejecutor de tools.

Defensa en profundidad:

* ``Gatekeeper.pre_tool_use`` corre como hook del SDK antes de cualquier otra regla de
  permisos (un ``deny`` del hook gana siempre, incluso en ``bypassPermissions``). Decide,
  pide aprobación si hace falta y emite un ticket de un solo uso.
* ``ToolExecutor.run`` envuelve cada handler: vuelve a comprobar kill switch y FORBIDDEN,
  y para SENSITIVE exige el ticket de esos parámetros exactos. Si el hook no se hubiese
  ejecutado por cualquier motivo, la acción sensible no se ejecuta.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from claude_agent_sdk import HookContext, HookMatcher, SdkMcpTool, create_sdk_mcp_server, tool
from claude_agent_sdk.types import HookEvent, McpSdkServerConfig

from marc_agent.audit import AuditLog
from marc_agent.notify import Notifier
from marc_agent.permissions.approvals import ApprovalBroker, TicketBook
from marc_agent.permissions.categories import Category, Decision, Verdict
from marc_agent.permissions.engine import AGENT_TOOL_NAMES, PolicyEngine
from marc_agent.permissions.state import ControlState
from marc_agent.security.taint import TurnTracker
from marc_agent.tools.base import ToolError, ToolOutput, ToolSpec
from marc_agent.tools.registry import SERVER_NAME, ToolRegistry
from marc_agent.util import truncate

log = logging.getLogger(__name__)


class Gatekeeper:
    def __init__(
        self,
        engine: PolicyEngine,
        approvals: ApprovalBroker,
        tickets: TicketBook,
        state: ControlState,
        turns: TurnTracker,
        notifier: Notifier,
        audit: AuditLog,
    ) -> None:
        self.engine = engine
        self.approvals = approvals
        self.tickets = tickets
        self.state = state
        self.turns = turns
        self.notifier = notifier
        self.audit = audit

    async def decide(
        self, tool_name: str, args: dict[str, Any], agent_id: str | None = None
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """Devuelve (permitido, motivo, input_modificado)."""
        decision = await self.engine.evaluate(tool_name, args, agent_id)
        turn = self.turns.current
        self.audit.tool_call(
            tool=tool_name,
            category=decision.category.value,
            decision=decision.verdict.value,
            args=args,
            reason=decision.reason,
            agent_id=agent_id,
            tainted=turn.tainted,
        )
        if decision.verdict is Verdict.ALLOW:
            if tool_name in AGENT_TOOL_NAMES:
                # Los subagentes deben devolver su resultado dentro del turno.
                return True, "", {**args, "run_in_background": False}
            return True, "", None
        if decision.verdict is Verdict.SIMULATE:
            await self._notify(f"🧪 DRY-RUN — no se ejecuta nada real.\nHaría: {decision.summary}")
            return (
                False,
                f"[DRY-RUN] Acción simulada, NO ejecutada: {decision.summary}. Informa a Marc de "
                "lo que se habría hecho.",
                None,
            )
        if decision.verdict is Verdict.ASK:
            approved = await self.approvals.request(
                decision.tool,
                args,
                decision.summary,
                tainted=turn.tainted,
                sources=sorted(turn.sources),
                signals=list(turn.injection_signals),
            )
            if approved:
                self.tickets.issue(decision.tool, args)
                self.audit.event("approval_granted", tool=decision.tool)
                return True, "", None
            self.audit.event("approval_denied", tool=decision.tool)
            return (
                False,
                "Marc no aprobó la acción (rechazada o sin respuesta en plazo). No la reintentes "
                "salvo que Marc lo pida de nuevo.",
                None,
            )
        # DENY
        if decision.notify_user:
            await self._notify(self._forbidden_message(decision))
        return False, decision.reason, None

    @staticmethod
    def _forbidden_message(decision: Decision) -> str:
        return (
            f"🚫 Acción bloqueada ({decision.category.value}).\n"
            f"{truncate(decision.summary or decision.tool, 600)}\n"
            f"Motivo: {truncate(decision.reason, 900)}"
        )

    async def _notify(self, text: str) -> None:
        try:
            await self.notifier.send(text)
        except Exception:
            log.warning("gate_notify_failed", extra={"event": "gate_notify_failed"})

    # -- integración con el Agent SDK ---------------------------------------------
    async def pre_tool_use(
        self, input_data: Any, tool_use_id: str | None, context: HookContext
    ) -> Any:
        if input_data.get("hook_event_name") != "PreToolUse":
            return {}
        tool_name = str(input_data.get("tool_name", ""))
        raw_args = input_data.get("tool_input") or {}
        args: dict[str, Any] = dict(raw_args) if isinstance(raw_args, dict) else {}
        agent_id = input_data.get("agent_id")
        try:
            allowed, reason, updated = await self.decide(tool_name, args, agent_id)
        except Exception:
            # Fallo interno del control ⇒ denegar (fail closed).
            log.exception("pre_tool_use_failed", extra={"event": "gate_error"})
            allowed, reason, updated = (
                False,
                "Error interno del control de permisos: denegado.",
                None,
            )
        output: dict[str, Any] = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow" if allowed else "deny",
        }
        if reason:
            output["permissionDecisionReason"] = reason
        if updated is not None:
            output["updatedInput"] = updated
        return {"hookSpecificOutput": output}

    def hook_matchers(self, timeout_s: float) -> dict[HookEvent, list[HookMatcher]]:
        return {
            "PreToolUse": [HookMatcher(matcher=None, hooks=[self.pre_tool_use], timeout=timeout_s)]
        }


class ToolExecutor:
    def __init__(self, gate: Gatekeeper, registry: ToolRegistry) -> None:
        self.gate = gate
        self.registry = registry

    async def guard(self, spec: ToolSpec, args: dict[str, Any]) -> str | None:
        """Segunda línea de defensa. Devuelve el motivo si no se debe ejecutar."""
        if self.gate.state.kill_switch:
            return "Kill switch activo (/stop): herramientas bloqueadas hasta /resume."
        decision = await self.gate.engine.evaluate(self.registry.full_name(spec.name), args)
        if decision.verdict is Verdict.DENY:
            return decision.reason or "Acción denegada."
        if decision.category is Category.SENSITIVE:
            if self.gate.state.dry_run:
                return f"[DRY-RUN] Acción simulada, NO ejecutada: {decision.summary}"
            if not self.gate.tickets.consume(spec.name, args):
                return (
                    "Acción sensible sin aprobación válida de Marc para estos parámetros exactos."
                )
        return None

    async def run(self, spec: ToolSpec, args: dict[str, Any]) -> ToolOutput:
        started = time.monotonic()
        turn = self.gate.turns.current
        refusal = await self.guard(spec, args)
        if refusal is not None:
            self.gate.audit.tool_call(
                tool=spec.name,
                category=spec.category.value,
                decision="blocked_at_executor",
                args=args,
                reason=refusal,
                tainted=turn.tainted,
                ok=False,
            )
            return ToolOutput(refusal, is_error=True)
        try:
            output = await spec.handler(args)
        except ToolError as exc:
            output = ToolOutput(str(exc), is_error=True)
        except Exception as exc:
            log.exception("tool_failed", extra={"event": "tool_failed", "tool": spec.name})
            output = ToolOutput(
                f"Error interno en {spec.name}: {type(exc).__name__}", is_error=True
            )
        if output.taint_source:
            turn.mark(output.taint_source, output.injection_signals)
        self.gate.audit.tool_call(
            tool=spec.name,
            category=spec.category.value,
            decision="executed",
            args=args,
            duration_ms=int((time.monotonic() - started) * 1000),
            tainted=turn.tainted,
            ok=not output.is_error,
        )
        return output

    def build_server(self) -> McpSdkServerConfig:
        tools: list[SdkMcpTool[Any]] = [self._wrap(spec) for spec in self.registry.specs()]
        return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=tools)

    def _wrap(self, spec: ToolSpec) -> SdkMcpTool[Any]:
        from claude_agent_sdk import ToolAnnotations

        annotations = ToolAnnotations(
            readOnlyHint=spec.read_only,
            destructiveHint=spec.category in (Category.SENSITIVE, Category.FORBIDDEN),
            openWorldHint=spec.category is Category.SENSITIVE,
        )

        async def handler(args: dict[str, Any]) -> dict[str, Any]:
            out = await self.run(spec, dict(args))
            return {"content": [{"type": "text", "text": out.text}], "is_error": out.is_error}

        return tool(spec.name, spec.description, spec.schema, annotations=annotations)(handler)
