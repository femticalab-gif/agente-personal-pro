"""Notas, tareas y contactos clave."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from marc_agent.db import Database, SearchIndex
from marc_agent.tools.base import ToolError
from marc_agent.util import iso, new_id, normalize, parse_dt, utcnow

TASK_STATUSES = ("pendiente", "en_curso", "hecha", "cancelada")


class NotesService:
    KIND = "note"

    def __init__(self, db: Database, index: SearchIndex) -> None:
        self._db = db
        self._index = index

    def _public(self, row: Any) -> dict[str, Any]:
        p = self._db.dec(row["payload"])
        return {"id": row["id"], "creada": row["created_at"][:10], **p}

    def create(
        self, title: str, body: str, project: str = "", tags: list[str] | None = None
    ) -> dict[str, Any]:
        if not title.strip():
            raise ToolError("La nota necesita título")
        note_id = new_id("n")
        now = iso(utcnow())
        payload = {
            "titulo": title.strip(),
            "texto": body,
            "proyecto": project,
            "etiquetas": tags or [],
        }
        self._db.execute(
            "INSERT INTO notes(id, created_at, updated_at, payload) VALUES(?,?,?,?)",
            (note_id, now, now, self._db.enc(payload)),
        )
        self._index.upsert(self.KIND, note_id, title, f"{body} {project} {' '.join(tags or [])}")
        return {"id": note_id, **payload}

    def get(self, note_id: str) -> dict[str, Any]:
        row = self._db.query_one("SELECT * FROM notes WHERE id=?", (note_id,))
        if row is None:
            raise ToolError(f"No existe la nota {note_id}")
        return self._public(row)

    def update(self, note_id: str, **fields: Any) -> dict[str, Any]:
        current = self.get(note_id)
        payload = {k: current[k] for k in ("titulo", "texto", "proyecto", "etiquetas")}
        for key, value in fields.items():
            if value is not None and key in payload:
                payload[key] = value
        self._db.execute(
            "UPDATE notes SET payload=?, updated_at=? WHERE id=?",
            (self._db.enc(payload), iso(utcnow()), note_id),
        )
        self._index.upsert(self.KIND, note_id, payload["titulo"], payload["texto"])
        return {"id": note_id, **payload}

    def delete(self, note_id: str) -> bool:
        self._index.delete(self.KIND, note_id)
        return self._db.execute("DELETE FROM notes WHERE id=?", (note_id,)).rowcount == 1

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        out = []
        for hit in self._index.search(query, kinds=[self.KIND], limit=limit):
            row = self._db.query_one("SELECT * FROM notes WHERE id=?", (hit.id,))
            if row is not None:
                note = self._public(row)
                note["texto"] = str(note.get("texto", ""))[:500]
                out.append(note)
        return out

    def recent(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._db.query("SELECT * FROM notes ORDER BY updated_at DESC LIMIT ?", (limit,))
        return [self._public(r) for r in rows]

    def rebuild_index(self) -> None:
        self._index.clear(self.KIND)
        for row in self._db.query("SELECT * FROM notes"):
            p = self._db.dec(row["payload"])
            self._index.upsert(self.KIND, row["id"], p["titulo"], p.get("texto", ""))


class TasksService:
    def __init__(self, db: Database, tz: ZoneInfo) -> None:
        self._db = db
        self._tz = tz

    def _public(self, row: Any) -> dict[str, Any]:
        p = self._db.dec(row["payload"])
        return {
            "id": row["id"],
            "estado": row["status"],
            "prioridad": row["priority"],
            "vence": row["due_at"],
            **p,
        }

    def create(
        self,
        title: str,
        due: str | None = None,
        priority: int = 2,
        project: str = "",
        notes: str = "",
    ) -> dict[str, Any]:
        if not title.strip():
            raise ToolError("La tarea necesita título")
        if priority not in (1, 2, 3):
            raise ToolError("Prioridad: 1 (alta), 2 (media) o 3 (baja)")
        due_iso = iso(parse_dt(due, self._tz)) if due else None
        task_id = new_id("t")
        now = iso(utcnow())
        payload = {"titulo": title.strip(), "proyecto": project, "notas": notes}
        self._db.execute(
            "INSERT INTO tasks(id, created_at, updated_at, status, priority, due_at, payload) "
            "VALUES(?,?,?,?,?,?,?)",
            (task_id, now, now, "pendiente", priority, due_iso, self._db.enc(payload)),
        )
        return self.get(task_id)

    def get(self, task_id: str) -> dict[str, Any]:
        row = self._db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if row is None:
            raise ToolError(f"No existe la tarea {task_id}")
        return self._public(row)

    def update(
        self,
        task_id: str,
        *,
        title: str | None = None,
        status: str | None = None,
        due: str | None = None,
        priority: int | None = None,
        project: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        row = self._db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if row is None:
            raise ToolError(f"No existe la tarea {task_id}")
        payload = self._db.dec(row["payload"])
        if title is not None:
            payload["titulo"] = title
        if project is not None:
            payload["proyecto"] = project
        if notes is not None:
            payload["notas"] = notes
        new_status = status or row["status"]
        if new_status not in TASK_STATUSES:
            raise ToolError(f"Estado inválido. Usa uno de {TASK_STATUSES}")
        new_priority = priority if priority is not None else row["priority"]
        due_iso = iso(parse_dt(due, self._tz)) if due else row["due_at"]
        self._db.execute(
            "UPDATE tasks SET status=?, priority=?, due_at=?, payload=?, updated_at=? WHERE id=?",
            (new_status, new_priority, due_iso, self._db.enc(payload), iso(utcnow()), task_id),
        )
        return self.get(task_id)

    def delete(self, task_id: str) -> bool:
        return self._db.execute("DELETE FROM tasks WHERE id=?", (task_id,)).rowcount == 1

    def list(
        self,
        status: str | None = "abiertas",
        project: str | None = None,
        due_before: datetime | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM tasks"
        params: list[Any] = []
        clauses: list[str] = []
        if status == "abiertas":
            clauses.append("status IN ('pendiente','en_curso')")
        elif status and status != "todas":
            clauses.append("status=?")
            params.append(status)
        if due_before is not None:
            clauses.append("due_at IS NOT NULL AND due_at<?")
            params.append(iso(due_before))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY priority, COALESCE(due_at, '9999') LIMIT 200"
        tasks = [self._public(r) for r in self._db.query(sql, params)]
        if project:
            wanted = normalize(project)
            tasks = [t for t in tasks if normalize(str(t.get("proyecto", ""))) == wanted]
        return tasks


class ContactsService:
    KIND = "contact"

    def __init__(self, db: Database, index: SearchIndex) -> None:
        self._db = db
        self._index = index

    def _public(self, row: Any) -> dict[str, Any]:
        return {"id": row["id"], **self._db.dec(row["payload"])}

    def upsert(
        self,
        name: str,
        relation: str,
        email: str = "",
        phone: str = "",
        notes: str = "",
        contact_id: str | None = None,
    ) -> dict[str, Any]:
        if not name.strip():
            raise ToolError("El contacto necesita nombre")
        payload = {
            "nombre": name.strip(),
            "relacion": relation,
            "email": email.strip().lower(),
            "telefono": phone,
            "notas": notes,
        }
        email_index = self._db.cipher.blind_index(payload["email"]) if payload["email"] else None
        now = iso(utcnow())
        if contact_id:
            cur = self._db.execute(
                "UPDATE contacts SET payload=?, email_index=?, updated_at=? WHERE id=?",
                (self._db.enc(payload), email_index, now, contact_id),
            )
            if cur.rowcount != 1:
                raise ToolError(f"No existe el contacto {contact_id}")
        else:
            contact_id = new_id("c")
            self._db.execute(
                "INSERT INTO contacts(id, created_at, updated_at, email_index, payload) "
                "VALUES(?,?,?,?,?)",
                (contact_id, now, now, email_index, self._db.enc(payload)),
            )
        self._index.upsert(
            self.KIND, contact_id, payload["nombre"], f"{relation} {payload['email']} {notes}"
        )
        return {"id": contact_id, **payload}

    def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        out = []
        for hit in self._index.search(query, kinds=[self.KIND], limit=limit):
            row = self._db.query_one("SELECT * FROM contacts WHERE id=?", (hit.id,))
            if row is not None:
                out.append(self._public(row))
        return out

    def all(self) -> list[dict[str, Any]]:
        return [self._public(r) for r in self._db.query("SELECT * FROM contacts")]

    def by_email(self, email: str) -> dict[str, Any] | None:
        row = self._db.query_one(
            "SELECT * FROM contacts WHERE email_index=?", (self._db.cipher.blind_index(email),)
        )
        return None if row is None else self._public(row)

    def delete(self, contact_id: str) -> bool:
        self._index.delete(self.KIND, contact_id)
        return self._db.execute("DELETE FROM contacts WHERE id=?", (contact_id,)).rowcount == 1

    def rebuild_index(self) -> None:
        self._index.clear(self.KIND)
        for row in self._db.query("SELECT * FROM contacts"):
            p = self._db.dec(row["payload"])
            self._index.upsert(
                self.KIND,
                row["id"],
                p["nombre"],
                f"{p.get('relacion', '')} {p.get('email', '')} {p.get('notas', '')}",
            )
