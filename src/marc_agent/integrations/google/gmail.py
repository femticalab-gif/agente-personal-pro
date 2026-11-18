"""Cliente de Gmail (REST v1) sobre ``GoogleApi``."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from email.message import EmailMessage as MimeMessage
from email.utils import getaddresses, parseaddr
from typing import Any

from marc_agent.integrations.google.auth import GoogleApi
from marc_agent.security.untrusted import html_to_text

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"


@dataclass
class EmailSummary:
    id: str
    thread_id: str
    sender: str
    subject: str
    date: str
    snippet: str
    labels: list[str] = field(default_factory=list)


@dataclass
class Email(EmailSummary):
    to: str = ""
    cc: str = ""
    body: str = ""
    hidden_content: bool = False
    message_id_header: str = ""
    references: str = ""

    @property
    def sender_address(self) -> str:
        return parseaddr(self.sender)[1].lower()


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    return {h["name"].lower(): h.get("value", "") for h in payload.get("headers", [])}


def _decode(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")


def _walk(part: dict[str, Any]) -> list[dict[str, Any]]:
    parts = [part]
    for child in part.get("parts", []) or []:
        parts.extend(_walk(child))
    return parts


def extract_body(payload: dict[str, Any]) -> tuple[str, bool]:
    """Texto del cuerpo. Prefiere text/plain; si solo hay HTML, lo convierte quitando lo oculto."""
    plain: list[str] = []
    html_parts: list[str] = []
    for part in _walk(payload):
        mime = part.get("mimeType", "")
        data = (part.get("body") or {}).get("data")
        if not data or part.get("filename"):
            continue
        if mime == "text/plain":
            plain.append(_decode(data))
        elif mime == "text/html":
            html_parts.append(_decode(data))
    hidden = False
    if html_parts:
        html_text, hidden = html_to_text("\n".join(html_parts))
        if not plain:
            return html_text, hidden
    return "\n".join(plain).strip(), hidden


def _summary(msg: dict[str, Any]) -> EmailSummary:
    h = _headers(msg.get("payload", {}))
    return EmailSummary(
        id=msg["id"],
        thread_id=msg.get("threadId", ""),
        sender=h.get("from", ""),
        subject=h.get("subject", ""),
        date=h.get("date", ""),
        snippet=msg.get("snippet", ""),
        labels=list(msg.get("labelIds", [])),
    )


class GmailClient:
    def __init__(self, api: GoogleApi) -> None:
        self._api = api

    async def search(self, query: str, max_results: int = 10) -> list[EmailSummary]:
        data = await self._api.json(
            "GET",
            f"{BASE}/messages",
            params={"q": query, "maxResults": max(1, min(max_results, 50))},
        )
        ids = [m["id"] for m in data.get("messages", [])]
        metas = await asyncio.gather(*(self._metadata(i) for i in ids))
        return list(metas)

    async def _metadata(self, message_id: str) -> EmailSummary:
        msg = await self._api.json(
            "GET",
            f"{BASE}/messages/{message_id}",
            params=[
                ("format", "metadata"),
                ("metadataHeaders", "From"),
                ("metadataHeaders", "Subject"),
                ("metadataHeaders", "Date"),
            ],
        )
        return _summary(msg)

    async def get(self, message_id: str) -> Email:
        msg = await self._api.json(
            "GET", f"{BASE}/messages/{message_id}", params={"format": "full"}
        )
        payload = msg.get("payload", {})
        h = _headers(payload)
        body, hidden = extract_body(payload)
        base = _summary(msg)
        return Email(
            **base.__dict__,
            to=h.get("to", ""),
            cc=h.get("cc", ""),
            body=body,
            hidden_content=hidden,
            message_id_header=h.get("message-id", ""),
            references=h.get("references", ""),
        )

    @staticmethod
    def build_mime(
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        reply_to: Email | None = None,
    ) -> str:
        msg = MimeMessage()
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        msg["Subject"] = subject
        if reply_to is not None and reply_to.message_id_header:
            msg["In-Reply-To"] = reply_to.message_id_header
            refs = f"{reply_to.references} {reply_to.message_id_header}".strip()
            msg["References"] = refs
        msg.set_content(body)
        return base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

    async def create_draft(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        reply_to: Email | None = None,
    ) -> str:
        message: dict[str, Any] = {"raw": self.build_mime(to, subject, body, cc, reply_to)}
        if reply_to is not None:
            message["threadId"] = reply_to.thread_id
        data = await self._api.json("POST", f"{BASE}/drafts", json={"message": message})
        return str(data.get("id", ""))

    async def send(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: list[str] | None = None,
        reply_to: Email | None = None,
    ) -> str:
        message: dict[str, Any] = {"raw": self.build_mime(to, subject, body, cc, reply_to)}
        if reply_to is not None:
            message["threadId"] = reply_to.thread_id
        data = await self._api.json("POST", f"{BASE}/messages/send", json=message)
        return str(data.get("id", ""))

    async def modify(self, message_id: str, add: list[str], remove: list[str]) -> None:
        await self._api.json(
            "POST",
            f"{BASE}/messages/{message_id}/modify",
            json={"addLabelIds": add, "removeLabelIds": remove},
        )

    async def labels(self) -> dict[str, str]:
        data = await self._api.json("GET", f"{BASE}/labels")
        return {lbl["name"]: lbl["id"] for lbl in data.get("labels", [])}

    async def ensure_label(self, name: str) -> str:
        existing = await self.labels()
        if name in existing:
            return existing[name]
        data = await self._api.json(
            "POST",
            f"{BASE}/labels",
            json={
                "name": name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )
        return str(data["id"])


def split_addresses(value: str | list[str]) -> list[str]:
    raw = value if isinstance(value, list) else [value]
    return [addr.lower() for _, addr in getaddresses(raw) if addr and "@" in addr]
