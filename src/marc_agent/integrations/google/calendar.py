"""Cliente de Google Calendar (calendario principal)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from marc_agent.integrations.google.auth import GoogleApi
from marc_agent.util import parse_dt

BASE = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


@dataclass
class CalEvent:
    id: str
    summary: str
    start: datetime
    end: datetime
    all_day: bool
    location: str = ""
    description: str = ""
    attendees: list[str] = field(default_factory=list)
    status: str = "confirmed"

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "summary": self.summary,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "all_day": self.all_day,
            "location": self.location,
            "attendees": self.attendees,
        }


def _parse_time(obj: dict[str, Any], tz: ZoneInfo) -> tuple[datetime, bool]:
    if "dateTime" in obj:
        return parse_dt(obj["dateTime"], tz), False
    d = date.fromisoformat(obj["date"])
    return datetime(d.year, d.month, d.day, tzinfo=tz), True


def parse_event(raw: dict[str, Any], tz: ZoneInfo) -> CalEvent:
    start, all_day = _parse_time(raw.get("start", {}), tz)
    end, _ = _parse_time(raw.get("end", raw.get("start", {})), tz)
    attendees = [
        a.get("email", "").lower()
        for a in raw.get("attendees", []) or []
        if a.get("email") and not a.get("self")
    ]
    return CalEvent(
        id=raw["id"],
        summary=raw.get("summary", "(sin título)"),
        start=start,
        end=end,
        all_day=all_day,
        location=raw.get("location", ""),
        description=raw.get("description", ""),
        attendees=attendees,
        status=raw.get("status", "confirmed"),
    )


def event_body(
    summary: str,
    start: datetime,
    end: datetime,
    tz: ZoneInfo,
    *,
    all_day: bool = False,
    description: str | None = None,
    location: str | None = None,
    attendees: list[str] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {"summary": summary}
    if all_day:
        body["start"] = {"date": start.date().isoformat()}
        body["end"] = {"date": end.date().isoformat()}
    else:
        body["start"] = {"dateTime": start.isoformat(), "timeZone": str(tz)}
        body["end"] = {"dateTime": end.isoformat(), "timeZone": str(tz)}
    if description is not None:
        body["description"] = description
    if location is not None:
        body["location"] = location
    if attendees is not None:
        body["attendees"] = [{"email": a} for a in attendees]
    return body


class CalendarClient:
    def __init__(self, api: GoogleApi, tz: ZoneInfo) -> None:
        self._api = api
        self._tz = tz

    async def list(
        self,
        time_min: datetime,
        time_max: datetime,
        query: str | None = None,
        max_results: int = 50,
    ) -> list[CalEvent]:
        params: dict[str, Any] = {
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": max(1, min(max_results, 250)),
        }
        if query:
            params["q"] = query
        data = await self._api.json("GET", BASE, params=params)
        return [
            parse_event(item, self._tz)
            for item in data.get("items", [])
            if item.get("status") != "cancelled"
        ]

    async def get(self, event_id: str) -> CalEvent:
        return parse_event(await self._api.json("GET", f"{BASE}/{event_id}"), self._tz)

    async def insert(self, body: dict[str, Any], notify_guests: bool) -> CalEvent:
        data = await self._api.json(
            "POST", BASE, params={"sendUpdates": "all" if notify_guests else "none"}, json=body
        )
        return parse_event(data, self._tz)

    async def patch(self, event_id: str, body: dict[str, Any], notify_guests: bool) -> CalEvent:
        data = await self._api.json(
            "PATCH",
            f"{BASE}/{event_id}",
            params={"sendUpdates": "all" if notify_guests else "none"},
            json=body,
        )
        return parse_event(data, self._tz)

    async def delete(self, event_id: str, notify_guests: bool) -> None:
        await self._api.request(
            "DELETE",
            f"{BASE}/{event_id}",
            params={"sendUpdates": "all" if notify_guests else "none"},
        )


def find_conflicts(events: list[CalEvent], start: datetime, end: datetime) -> list[CalEvent]:
    return [e for e in events if not e.all_day and e.start < end and start < e.end]
