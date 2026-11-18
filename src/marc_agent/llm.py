"""Llamadas directas a la API de Claude (fuera del bucle del agente).

Se usan para tareas acotadas con salida estructurada: clasificación (contenido económico,
fraude, triaje de correo), resúmenes y extracción de datos de fotos/PDF (visión).
Siempre pasan por el control de presupuesto y registran el consumo.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol, cast

import anthropic
from anthropic.types import Message, MessageParam, OutputConfigParam, TextBlockParam

from marc_agent.budget import BudgetExceededError, BudgetTracker

log = logging.getLogger(__name__)


class LLMUnavailableError(RuntimeError):
    """El modelo no está disponible (red, presupuesto, rechazo o respuesta inválida)."""


class LLM(Protocol):
    async def json(
        self,
        *,
        purpose: str,
        system: str,
        content: str | list[dict[str, Any]],
        schema: dict[str, Any],
        model: str | None = None,
        max_tokens: int = 1024,
        effort: str = "low",
    ) -> dict[str, Any]: ...

    async def text(
        self,
        *,
        purpose: str,
        system: str,
        content: str | list[dict[str, Any]],
        model: str | None = None,
        max_tokens: int = 2048,
        effort: str = "low",
    ) -> str: ...


class AnthropicLLM:
    def __init__(
        self,
        client: anthropic.AsyncAnthropic,
        default_model: str,
        budget: BudgetTracker,
    ) -> None:
        self._client = client
        self._default_model = default_model
        self._budget = budget

    async def _create(
        self,
        *,
        purpose: str,
        system: str,
        content: str | list[dict[str, Any]],
        model: str | None,
        max_tokens: int,
        output_config: dict[str, Any],
    ) -> Message:
        chosen = model or self._default_model
        try:
            self._budget.ensure_available()
        except BudgetExceededError as exc:
            raise LLMUnavailableError(str(exc)) from exc
        system_blocks: list[TextBlockParam] = [
            {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
        ]
        messages = cast(list[MessageParam], [{"role": "user", "content": content}])
        try:
            response: Message = await self._client.messages.create(
                model=chosen,
                max_tokens=max_tokens,
                system=system_blocks,
                messages=messages,
                output_config=cast(OutputConfigParam, output_config),
            )
        except anthropic.RateLimitError as exc:
            raise LLMUnavailableError("Límite de peticiones de la API") from exc
        except anthropic.APIStatusError as exc:
            raise LLMUnavailableError(f"Error de la API ({exc.status_code})") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMUnavailableError("Sin conexión con la API") from exc
        usage = response.usage
        await self._budget.record(
            model=chosen,
            kind=purpose,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read=usage.cache_read_input_tokens or 0,
            cache_write=usage.cache_creation_input_tokens or 0,
        )
        if response.stop_reason == "refusal":
            raise LLMUnavailableError("El modelo rechazó la petición")
        return response

    @staticmethod
    def _first_text(response: Message) -> str:
        for block in response.content:
            if block.type == "text":
                return block.text
        raise LLMUnavailableError("Respuesta sin texto")

    async def json(
        self,
        *,
        purpose: str,
        system: str,
        content: str | list[dict[str, Any]],
        schema: dict[str, Any],
        model: str | None = None,
        max_tokens: int = 1024,
        effort: str = "low",
    ) -> dict[str, Any]:
        response = await self._create(
            purpose=purpose,
            system=system,
            content=content,
            model=model,
            max_tokens=max_tokens,
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        )
        if response.stop_reason == "max_tokens":
            raise LLMUnavailableError("Respuesta truncada")
        try:
            data = json.loads(self._first_text(response))
        except json.JSONDecodeError as exc:
            raise LLMUnavailableError("JSON inválido del modelo") from exc
        if not isinstance(data, dict):
            raise LLMUnavailableError("Se esperaba un objeto JSON")
        return data

    async def text(
        self,
        *,
        purpose: str,
        system: str,
        content: str | list[dict[str, Any]],
        model: str | None = None,
        max_tokens: int = 2048,
        effort: str = "low",
    ) -> str:
        response = await self._create(
            purpose=purpose,
            system=system,
            content=content,
            model=model,
            max_tokens=max_tokens,
            output_config={"effort": effort},
        )
        return self._first_text(response).strip()
