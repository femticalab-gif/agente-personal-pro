"""Catálogo completo de herramientas propias. Nada de skills ni MCP de terceros."""

from __future__ import annotations

from marc_agent.services import Services
from marc_agent.tools import (
    calendar_tools,
    docs_tools,
    finance_tools,
    gmail_tools,
    health_tools,
    misc_tools,
    productivity_tools,
)
from marc_agent.tools.registry import ToolRegistry


def build_registry(svc: Services) -> ToolRegistry:
    registry = ToolRegistry()
    for module in (
        gmail_tools,
        calendar_tools,
        docs_tools,
        finance_tools,
        health_tools,
        productivity_tools,
        misc_tools,
    ):
        registry.extend(module.build(svc))
    return registry
