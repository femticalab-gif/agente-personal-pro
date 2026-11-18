"""Seguimiento de "contaminación" del turno.

Si durante un turno el agente lee contenido externo (correo, documento, web,
transcripción, mensaje reenviado), el turno queda marcado. Consecuencias:

* Toda acción SENSITIVE posterior exige aprobación explícita (nunca hay atajos) y la
  petición de aprobación lo advierte.
* Lo que se guarde en memoria hereda la procedencia externa y se marca como no fiable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from marc_agent.util import new_id


@dataclass
class TurnState:
    turn_id: str = field(default_factory=lambda: new_id("turn"))
    tainted: bool = False
    sources: set[str] = field(default_factory=set)
    injection_signals: list[str] = field(default_factory=list)
    background: bool = False

    def mark(self, source: str, signals: list[str] | None = None) -> None:
        self.tainted = True
        self.sources.add(source)
        if signals:
            self.injection_signals.extend(s for s in signals if s not in self.injection_signals)

    @property
    def primary_source(self) -> str | None:
        for preferred in ("email", "doc", "web", "transcripcion", "reenviado"):
            if preferred in self.sources:
                return preferred
        return next(iter(self.sources), None)


class TurnTracker:
    """Un único turno activo a la vez (el agente es mono-usuario y procesa en serie)."""

    def __init__(self) -> None:
        self._current: TurnState | None = None
        self._background = TurnState(turn_id="background", background=True)

    def start(self, initial_sources: list[str] | None = None) -> TurnState:
        state = TurnState()
        for src in initial_sources or []:
            state.mark(src)
        self._current = state
        return state

    def end(self) -> None:
        self._current = None

    @property
    def current(self) -> TurnState:
        # Fuera de un turno (scheduler, botones) las tools usan un estado de fondo que
        # se considera contaminado por prudencia si alguien leyó contenido externo.
        return self._current or self._background
