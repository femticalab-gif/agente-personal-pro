"""Aprobaciones humanas por Telegram y tickets de ejecución de un solo uso."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from marc_agent.crypto import canonical_json, sha256_hex
from marc_agent.db import Database
from marc_agent.notify import Button, Notifier
from marc_agent.util import iso, new_id, truncate, utcnow

log = logging.getLogger(__name__)

_MAX_INLINE = 3300


@dataclass
class PendingApproval:
    id: str
    tool: str
    summary: str
    future: asyncio.Future[bool]
    message_id: int | None = None
    created: float = field(default_factory=time.monotonic)


class ApprovalBroker:
    """Pide aprobación a Marc y espera su respuesta. Sin respuesta en plazo ⇒ rechazo."""

    def __init__(self, db: Database, notifier: Notifier, timeout_s: float) -> None:
        self._db = db
        self.notifier = notifier
        self._timeout = timeout_s
        self._pending: dict[str, PendingApproval] = {}

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def _format(
        self,
        tool: str,
        summary: str,
        args: dict[str, Any],
        tainted: bool,
        sources: list[str],
        signals: list[str],
    ) -> tuple[str, bool]:
        params = json.dumps(args, ensure_ascii=False, indent=1, default=str)
        lines = ["🔐 Aprobación requerida", "", f"Acción: {summary}", f"Herramienta: {tool}"]
        if tainted:
            lines.append(
                "⚠️ En este turno se leyó contenido externo ("
                + ", ".join(sorted(sources))
                + "). Revisa que la acción la pediste tú."
            )
        if signals:
            lines.append("🚨 Señales de prompt injection: " + ", ".join(signals))
        minutes = int(self._timeout // 60)
        lines += ["", "Parámetros exactos:", params, "", f"Caduca en {minutes} min (= rechazo)."]
        text = "\n".join(lines)
        if len(text) <= _MAX_INLINE:
            return text, False
        short = "\n".join(
            [
                *lines[:4],
                "",
                "Parámetros completos en el documento adjunto.",
                "",
                f"Caduca en {minutes} min (= rechazo).",
            ]
        )
        return short, True

    async def request(
        self,
        tool: str,
        args: dict[str, Any],
        summary: str,
        *,
        tainted: bool = False,
        sources: list[str] | None = None,
        signals: list[str] | None = None,
    ) -> bool:
        approval_id = new_id("apr")
        loop = asyncio.get_running_loop()
        pending = PendingApproval(approval_id, tool, summary, loop.create_future())
        self._pending[approval_id] = pending
        self._db.execute(
            "INSERT INTO approvals(id, created_at, status, tool, payload) VALUES(?,?,?,?,?)",
            (
                approval_id,
                iso(utcnow()),
                "pending",
                tool,
                self._db.enc({"summary": summary, "args": args, "tainted": tainted}),
            ),
        )
        text, as_document = self._format(tool, summary, args, tainted, sources or [], signals or [])
        buttons = [
            [
                Button("✅ Aprobar", f"apr:{approval_id}:y"),
                Button("❌ Rechazar", f"apr:{approval_id}:n"),
            ]
        ]
        status = "rejected"
        try:
            if as_document:
                full = json.dumps(args, ensure_ascii=False, indent=2, default=str)
                await self.notifier.send_document(
                    full.encode("utf-8"), f"{approval_id}.json", caption=truncate(summary, 900)
                )
            pending.message_id = await self.notifier.send(text, buttons)
            approved = await asyncio.wait_for(asyncio.shield(pending.future), self._timeout)
            status = "approved" if approved else "rejected"
            return approved
        except TimeoutError:
            status = "timeout"
            if pending.message_id is not None:
                await self._safe_edit(
                    pending.message_id,
                    f"⏱ Sin respuesta en {int(self._timeout // 60)} min: RECHAZADA.\n{summary}",
                )
            return False
        except Exception:
            log.exception("approval_request_failed", extra={"event": "approval_error"})
            status = "error"
            return False
        finally:
            self._pending.pop(approval_id, None)
            self._db.execute(
                "UPDATE approvals SET status=?, decided_at=? WHERE id=?",
                (status, iso(utcnow()), approval_id),
            )

    async def resolve(self, approval_id: str, approved: bool) -> str:
        """Llamado desde el botón de Telegram. Devuelve texto para el usuario."""
        pending = self._pending.get(approval_id)
        if pending is None or pending.future.done():
            return "Esta aprobación ya no está activa."
        pending.future.set_result(approved)
        if pending.message_id is not None:
            mark = "✅ APROBADA" if approved else "❌ RECHAZADA"
            await self._safe_edit(pending.message_id, f"{mark}\n{pending.summary}")
        return "Aprobada" if approved else "Rechazada"

    async def reject_all(self, reason: str) -> int:
        count = 0
        for pending in list(self._pending.values()):
            if not pending.future.done():
                pending.future.set_result(False)
                count += 1
                if pending.message_id is not None:
                    await self._safe_edit(
                        pending.message_id, f"❌ RECHAZADA ({reason})\n{pending.summary}"
                    )
        return count

    async def _safe_edit(self, message_id: int, text: str) -> None:
        try:
            await self.notifier.edit(message_id, text)
        except Exception:
            log.warning("approval_edit_failed", extra={"event": "approval_edit_failed"})


class TicketBook:
    """Tickets de un solo uso que ligan una aprobación a unos parámetros exactos.

    El hook emite el ticket tras la aprobación; el ejecutor de la tool lo consume. Si los
    parámetros cambian (o no hubo aprobación), no hay ticket y la acción no se ejecuta.
    """

    def __init__(self, ttl_s: float = 180.0) -> None:
        self._ttl = ttl_s
        self._tickets: dict[str, float] = {}

    @staticmethod
    def _key(tool: str, args: dict[str, Any]) -> str:
        return sha256_hex(tool + "\x00" + canonical_json(args))

    def issue(self, tool: str, args: dict[str, Any]) -> None:
        self._purge()
        self._tickets[self._key(tool, args)] = time.monotonic() + self._ttl

    def consume(self, tool: str, args: dict[str, Any]) -> bool:
        self._purge()
        return self._tickets.pop(self._key(tool, args), None) is not None

    def clear(self) -> None:
        self._tickets.clear()

    def _purge(self) -> None:
        now = time.monotonic()
        for key in [k for k, exp in self._tickets.items() if exp <= now]:
            del self._tickets[key]
