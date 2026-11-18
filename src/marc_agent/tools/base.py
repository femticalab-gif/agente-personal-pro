"""Tipos base del catálogo de herramientas propias."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from marc_agent.permissions.categories import Category

JsonDict = dict[str, Any]


class ToolError(RuntimeError):
    """Error controlado de una tool: el mensaje se devuelve tal cual al modelo."""


@dataclass
class ToolOutput:
    text: str
    is_error: bool = False
    # Si la salida contiene contenido externo, origen para marcar el turno como contaminado.
    taint_source: str | None = None
    injection_signals: list[str] = field(default_factory=list)

    @classmethod
    def data(cls, payload: Any, **kwargs: Any) -> ToolOutput:
        return cls(json.dumps(payload, ensure_ascii=False, indent=1, default=str), **kwargs)


Handler = Callable[[JsonDict], Awaitable[ToolOutput]]
Classifier = Callable[[JsonDict], Awaitable[Category]]
TextExtractor = Callable[[JsonDict], Awaitable[str | None]]
ReasonCheck = Callable[[JsonDict], Awaitable[str | None]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    schema: JsonDict
    handler: Handler
    category: Category
    # Categoría dinámica (p. ej. un evento es SENSITIVE solo si tiene invitados).
    classify: Classifier | None = None
    # Texto saliente que el clasificador económico/salud debe revisar (None = no aplica).
    outbound_text: TextExtractor | None = None
    # Comprobación FORBIDDEN adicional (p. ej. responder a un correo marcado como fraude).
    forbidden_check: ReasonCheck | None = None
    # Descripción legible de la acción exacta para la petición de aprobación.
    describe: Callable[[JsonDict], str] | None = None
    read_only: bool = False
    subagent_ok: bool = False

    async def category_for(self, args: JsonDict) -> Category:
        if self.classify is not None:
            return await self.classify(args)
        return self.category

    def summary(self, args: JsonDict) -> str:
        if self.describe is not None:
            return self.describe(args)
        return f"{self.name}({json.dumps(args, ensure_ascii=False, default=str)})"


def obj_schema(properties: JsonDict, required: list[str] | None = None) -> JsonDict:
    return {
        "type": "object",
        "properties": properties,
        "required": required or [],
        "additionalProperties": False,
    }


def s(description: str, **extra: Any) -> JsonDict:
    return {"type": "string", "description": description, **extra}


def i(description: str, **extra: Any) -> JsonDict:
    return {"type": "integer", "description": description, **extra}


def n(description: str, **extra: Any) -> JsonDict:
    return {"type": "number", "description": description, **extra}


def b(description: str) -> JsonDict:
    return {"type": "boolean", "description": description}


def arr(description: str, items: JsonDict | None = None, **extra: Any) -> JsonDict:
    return {
        "type": "array",
        "description": description,
        "items": items or {"type": "string"},
        **extra,
    }
