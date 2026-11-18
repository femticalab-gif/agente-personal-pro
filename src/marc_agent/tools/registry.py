"""Registro del catálogo de herramientas propias (servidor MCP in-process ``marc``)."""

from __future__ import annotations

from collections.abc import Iterable

from marc_agent.tools.base import ToolSpec

SERVER_NAME = "marc"
PREFIX = f"mcp__{SERVER_NAME}__"


class ToolRegistry:
    def __init__(self, specs: Iterable[ToolSpec] = ()) -> None:
        self._specs: dict[str, ToolSpec] = {}
        for spec in specs:
            self.add(spec)

    def add(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"Tool duplicada: {spec.name}")
        if not spec.name.replace("_", "").isalnum():
            raise ValueError(f"Nombre de tool inválido: {spec.name}")
        self._specs[spec.name] = spec

    def extend(self, specs: Iterable[ToolSpec]) -> None:
        for spec in specs:
            self.add(spec)

    @staticmethod
    def full_name(name: str) -> str:
        return f"{PREFIX}{name}"

    @staticmethod
    def short_name(name: str) -> str | None:
        """Nombre corto si es una tool de nuestro servidor; ``None`` en otro caso."""
        if name.startswith(PREFIX):
            return name[len(PREFIX) :]
        return None

    def get(self, name: str) -> ToolSpec | None:
        short = self.short_name(name)
        return self._specs.get(short if short is not None else name)

    def resolve_external(self, tool_name: str) -> ToolSpec | None:
        """Resuelve un nombre tal como llega del CLI: solo ``mcp__marc__*`` es válido."""
        short = self.short_name(tool_name)
        return None if short is None else self._specs.get(short)

    def names(self) -> list[str]:
        return sorted(self._specs)

    def specs(self) -> list[ToolSpec]:
        return [self._specs[n] for n in self.names()]

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and self.get(name) is not None
