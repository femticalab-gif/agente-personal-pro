"""Estado de control persistente: kill switch (/stop) y dry-run."""

from __future__ import annotations

from marc_agent.db import Database


class ControlState:
    def __init__(self, db: Database, dry_run_default: bool = False) -> None:
        self._db = db
        if self._db.get_state("dry_run") is None:
            self._db.set_state("dry_run", "1" if dry_run_default else "0")

    @property
    def kill_switch(self) -> bool:
        return self._db.get_state("kill_switch", "0") == "1"

    @kill_switch.setter
    def kill_switch(self, value: bool) -> None:
        self._db.set_state("kill_switch", "1" if value else "0")

    @property
    def dry_run(self) -> bool:
        return self._db.get_state("dry_run", "0") == "1"

    @dry_run.setter
    def dry_run(self, value: bool) -> None:
        self._db.set_state("dry_run", "1" if value else "0")
