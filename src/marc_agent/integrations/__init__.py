"""Integraciones externas (todas por HTTPS a través del proxy de egress)."""

from __future__ import annotations

import httpx

from marc_agent.tools.base import ToolError


class IntegrationError(ToolError):
    """Fallo de una integración externa (mensaje apto para el modelo y el usuario)."""


class NotConfiguredError(IntegrationError):
    """Integración sin credenciales: el usuario debe completar la configuración."""


def make_http_client(proxy_url: str | None, timeout_s: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        proxy=proxy_url,
        timeout=httpx.Timeout(timeout_s, connect=10.0),
        follow_redirects=False,
        headers={"User-Agent": "marc-agent/1.0"},
        trust_env=False,
    )
