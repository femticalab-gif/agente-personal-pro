"""Preferencias, estado de onboarding y registro cifrado de la conversación."""

from __future__ import annotations

from typing import Any

from marc_agent.db import Database
from marc_agent.tools.base import ToolError
from marc_agent.util import iso, new_id, utcnow

ONBOARDING_SECTIONS = (
    "prestamos_y_cuotas",
    "tratamientos_y_pautas",
    "citas",
    "contactos_clave",
    "preferencias_briefing",
)

DEFAULT_PREFS: dict[str, Any] = {
    "briefing_secciones": ["agenda", "correo", "vencimientos", "salud", "tareas", "clima"],
    "detalle_salud_finanzas": "resumen",
    "idioma": "es",
    "avisos_correo_inmediatos": ["citas", "facturas", "banco", "urgente", "fraude"],
}
ALLOWED_PREF_KEYS = frozenset(DEFAULT_PREFS)


class ProfileService:
    def __init__(self, db: Database) -> None:
        self._db = db

    # -- preferencias ----------------------------------------------------------------
    def prefs(self) -> dict[str, Any]:
        stored = self._db.get_secure("prefs", {}) or {}
        return {**DEFAULT_PREFS, **stored}

    def set_pref(self, key: str, value: Any) -> dict[str, Any]:
        if key not in ALLOWED_PREF_KEYS:
            raise ToolError(f"Preferencia desconocida. Válidas: {sorted(ALLOWED_PREF_KEYS)}")
        stored = self._db.get_secure("prefs", {}) or {}
        stored[key] = value
        self._db.set_secure("prefs", stored)
        return self.prefs()

    # -- onboarding ------------------------------------------------------------------
    def onboarding_done(self) -> list[str]:
        return list(self._db.get_secure("onboarding_done", []) or [])

    def onboarding_pending(self) -> list[str]:
        done = set(self.onboarding_done())
        return [s for s in ONBOARDING_SECTIONS if s not in done]

    def mark_onboarding(self, section: str) -> list[str]:
        if section not in ONBOARDING_SECTIONS:
            raise ToolError(f"Sección desconocida. Válidas: {ONBOARDING_SECTIONS}")
        done = self.onboarding_done()
        if section not in done:
            done.append(section)
            self._db.set_secure("onboarding_done", done)
        return self.onboarding_pending()

    def reset_onboarding(self) -> None:
        self._db.set_secure("onboarding_done", [])


class ChatLog:
    """Registro cifrado de turnos (para resumir al cerrar la sesión) y resúmenes."""

    def __init__(self, db: Database) -> None:
        self._db = db

    def add_turn(self, session_id: str, user: str, assistant: str) -> None:
        self._db.execute(
            "INSERT INTO chat_turns(session_id, created_at, payload) VALUES(?,?,?)",
            (session_id, iso(utcnow()), self._db.enc({"u": user[:8000], "a": assistant[:8000]})),
        )

    def turns(self, session_id: str) -> list[dict[str, str]]:
        rows = self._db.query(
            "SELECT payload FROM chat_turns WHERE session_id=? ORDER BY id", (session_id,)
        )
        return [self._db.dec(r["payload"]) for r in rows]

    def clear_turns(self, session_id: str) -> None:
        self._db.execute("DELETE FROM chat_turns WHERE session_id=?", (session_id,))

    def add_summary(self, session_id: str, summary: str) -> str:
        summary_id = new_id("sum")
        self._db.execute(
            "INSERT INTO chat_summaries(id, created_at, session_id, payload) VALUES(?,?,?,?)",
            (summary_id, iso(utcnow()), session_id, self._db.enc({"text": summary})),
        )
        # Retención: solo los 30 resúmenes más recientes.
        self._db.execute(
            "DELETE FROM chat_summaries WHERE id NOT IN "
            "(SELECT id FROM chat_summaries ORDER BY created_at DESC LIMIT 30)"
        )
        return summary_id

    def recent_summaries(self, limit: int = 3) -> list[tuple[str, str]]:
        rows = self._db.query(
            "SELECT created_at, payload FROM chat_summaries ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
        return [(r["created_at"][:16], self._db.dec(r["payload"])["text"]) for r in reversed(rows)]
