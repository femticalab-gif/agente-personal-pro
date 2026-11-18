"""Dobles de prueba: LLM, notificador, API de Telegram y cliente del Agent SDK."""

from __future__ import annotations

import itertools
import json
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
)

from marc_agent.llm import LLMUnavailableError
from marc_agent.notify import Keyboard


class FakeLLM:
    """LLM programable por ``purpose``. Sin respuesta configurada ⇒ LLMUnavailableError."""

    def __init__(self) -> None:
        self.responses: dict[str, Any] = {}
        self.calls: list[dict[str, Any]] = []
        self.fail = False

    def set(self, purpose: str, response: Any) -> None:
        self.responses[purpose] = response

    async def _respond(self, purpose: str, kwargs: dict[str, Any]) -> Any:
        self.calls.append({"purpose": purpose, **kwargs})
        if self.fail or purpose not in self.responses:
            raise LLMUnavailableError(f"sin respuesta fake para {purpose}")
        value = self.responses[purpose]
        return value(kwargs) if callable(value) else value

    async def json(self, *, purpose: str, **kwargs: Any) -> dict[str, Any]:
        result = await self._respond(purpose, kwargs)
        assert isinstance(result, dict)
        return result

    async def text(self, *, purpose: str, **kwargs: Any) -> str:
        return str(await self._respond(purpose, kwargs))


@dataclass
class Sent:
    text: str
    buttons: Keyboard | None
    message_id: int


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[Sent] = []
        self.edits: list[tuple[int, str]] = []
        self.documents: list[tuple[bytes, str, str]] = []
        self._ids = itertools.count(1000)
        self.on_send: Callable[[Sent], Awaitable[None]] | None = None

    async def send(self, text: str, buttons: Keyboard | None = None) -> int | None:
        msg = Sent(text, buttons, next(self._ids))
        self.sent.append(msg)
        if self.on_send is not None:
            await self.on_send(msg)
        return msg.message_id

    async def edit(self, message_id: int, text: str, buttons: Keyboard | None = None) -> None:
        self.edits.append((message_id, text))

    async def send_document(self, data: bytes, filename: str, caption: str = "") -> None:
        self.documents.append((data, filename, caption))

    def texts(self) -> str:
        return "\n---\n".join(s.text for s in self.sent)

    def approvals(self) -> list[Sent]:
        return [s for s in self.sent if s.buttons and s.buttons[0][0].data.startswith("apr:")]


class FakeTelegramApi:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, Keyboard | None]] = []
        self.edits: list[tuple[int, int, str]] = []
        self.documents: list[tuple[int, bytes, str, str]] = []
        self.callbacks: list[tuple[str, str]] = []
        self.files: dict[str, bytes] = {}
        self.deleted: list[int] = []
        self._ids = itertools.count(1)

    async def send_message(self, chat_id: int, text: str, buttons: Keyboard | None = None) -> int:
        self.messages.append((chat_id, text, buttons))
        return next(self._ids)

    async def edit_message(
        self, chat_id: int, message_id: int, text: str, buttons: Keyboard | None = None
    ) -> None:
        self.edits.append((chat_id, message_id, text))

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append(message_id)

    async def send_document(
        self, chat_id: int, data: bytes, filename: str, caption: str = ""
    ) -> None:
        self.documents.append((chat_id, data, filename, caption))

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.callbacks.append((callback_id, text))

    async def download(self, file_id: str) -> bytes:
        return self.files[file_id]

    async def typing(self, chat_id: int) -> None:
        return None

    def all_text(self) -> str:
        return "\n---\n".join([m[1] for m in self.messages] + [e[2] for e in self.edits])


# -- cliente falso del Agent SDK -----------------------------------------------------------
@dataclass
class ToolCall:
    name: str
    args: dict[str, Any]
    agent_id: str | None = None


@dataclass
class Say:
    text: str


Step = ToolCall | Say
Script = list[Step] | Callable[[str], list[Step]]


@dataclass
class ToolTrace:
    name: str
    allowed: bool
    reason: str
    output: str | None = None
    is_error: bool | None = None


@dataclass
class FakeAgentState:
    """Estado compartido entre instancias (para inspeccionar desde los tests)."""

    scripts: list[Script] = field(default_factory=list)
    prompts: list[Any] = field(default_factory=list)
    traces: list[ToolTrace] = field(default_factory=list)
    options: list[ClaudeAgentOptions] = field(default_factory=list)
    connects: int = 0
    interrupts: int = 0
    disconnects: int = 0
    cost_per_turn: float = 0.01
    session_id: str = "sess-1"


class FakeAgentClient:
    """Imita al CLI: por cada llamada a herramienta ejecuta el hook PreToolUse y, si
    permite, el handler del servidor MCP in-process (vía ``tool_runner``)."""

    def __init__(
        self,
        options: ClaudeAgentOptions,
        state: FakeAgentState,
        tool_runner: Callable[[str, dict[str, Any]], Awaitable[tuple[str, bool]]],
    ) -> None:
        self.options = options
        self.state = state
        self._runner = tool_runner
        self._pending: list[Step] = []
        self._prompt_text = ""
        self._cost = 0.0
        state.options.append(options)

    async def connect(self, prompt: Any = None) -> None:
        self.state.connects += 1

    async def query(
        self, prompt: str | AsyncIterable[dict[str, Any]], session_id: str = "default"
    ) -> None:
        if isinstance(prompt, str):
            text = prompt
            self.state.prompts.append(prompt)
        else:
            text = ""
            async for msg in prompt:
                self.state.prompts.append(msg)
                for block in msg["message"]["content"]:
                    if block.get("type") == "text":
                        text += block["text"]
        self._prompt_text = text
        script = self.state.scripts.pop(0) if self.state.scripts else [Say("ok")]
        self._pending = script(text) if callable(script) else list(script)

    async def _hook(self, call: ToolCall, tool_use_id: str) -> dict[str, Any]:
        hooks = self.options.hooks or {}
        decision: dict[str, Any] = {}
        for matcher in hooks.get("PreToolUse", []):
            for hook in matcher.hooks:
                data = {
                    "hook_event_name": "PreToolUse",
                    "tool_name": call.name,
                    "tool_input": call.args,
                    "tool_use_id": tool_use_id,
                    "session_id": self.state.session_id,
                    "cwd": "/",
                    "transcript_path": "",
                }
                if call.agent_id:
                    data["agent_id"] = call.agent_id
                decision = await hook(data, tool_use_id, {"signal": None})
        return decision

    async def receive_response(self) -> AsyncIterator[Any]:
        texts: list[str] = []
        for idx, step in enumerate(self._pending):
            if isinstance(step, Say):
                yield StreamEvent(
                    uuid=f"u{idx}",
                    session_id=self.state.session_id,
                    event={
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": step.text},
                    },
                )
                yield AssistantMessage(content=[TextBlock(step.text)], model="fake")
                texts.append(step.text)
                continue
            tool_use_id = f"tu{idx}"
            yield AssistantMessage(
                content=[ToolUseBlock(tool_use_id, step.name, step.args)], model="fake"
            )
            out = await self._hook(step, tool_use_id)
            spec = out.get("hookSpecificOutput", {})
            allowed = spec.get("permissionDecision") == "allow"
            reason = spec.get("permissionDecisionReason", "")
            if allowed:
                args = spec.get("updatedInput") or step.args
                if step.name.startswith("mcp__marc__"):
                    text, is_error = await self._runner(step.name, args)
                else:
                    text, is_error = "subagente ejecutado", False
                self.state.traces.append(ToolTrace(step.name, True, reason, text, is_error))
            else:
                self.state.traces.append(ToolTrace(step.name, False, reason))
        self._cost += self.state.cost_per_turn
        final = texts[-1] if texts else ""
        yield ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id=self.state.session_id,
            total_cost_usd=self._cost,
            usage={"input_tokens": 100, "output_tokens": 20},
            result=final,
        )

    async def interrupt(self) -> None:
        self.state.interrupts += 1

    async def disconnect(self) -> None:
        self.state.disconnects += 1


def json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


@dataclass
class CaptureSink:
    deltas: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    final: str = ""
    failed: bool = False

    async def on_text_delta(self, text: str) -> None:
        self.deltas.append(text)

    async def on_tool_use(self, tool_name: str) -> None:
        self.tools.append(tool_name)

    async def finish(self, text: str) -> None:
        self.final = text

    async def fail(self, text: str) -> None:
        self.final = text
        self.failed = True
