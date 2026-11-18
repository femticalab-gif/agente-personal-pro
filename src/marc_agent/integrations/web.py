"""Búsqueda web (Brave Search API), clima (Open-Meteo) y transcripción (Google STT).

La búsqueda devuelve solo títulos, URLs y descripciones: nunca se descargan páginas.
"""

from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass
from typing import Any

import httpx

from marc_agent.integrations import IntegrationError, NotConfiguredError

BRAVE_URL = "https://api.search.brave.com/res/v1/web/search"
OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
STT_SYNC_URL = "https://speech.googleapis.com/v1/speech:recognize"
STT_ASYNC_URL = "https://speech.googleapis.com/v1/speech:longrunningrecognize"
STT_OPERATION_URL = "https://speech.googleapis.com/v1/operations/"


@dataclass(frozen=True)
class SearchResult:
    title: str
    url: str
    description: str
    age: str = ""


class BraveSearch:
    def __init__(self, http: httpx.AsyncClient, api_key: str) -> None:
        self._http = http
        self._key = api_key

    async def search(self, query: str, count: int = 5) -> list[SearchResult]:
        if not self._key:
            raise NotConfiguredError("Falta BRAVE_API_KEY en el .env")
        try:
            response = await self._http.get(
                BRAVE_URL,
                params={
                    "q": query[:380],
                    "count": max(1, min(count, 10)),
                    "country": "es",
                    "search_lang": "es",
                    "safesearch": "moderate",
                    "text_decorations": "false",
                },
                headers={"X-Subscription-Token": self._key, "Accept": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise IntegrationError(f"Error de red en la búsqueda: {type(exc).__name__}") from exc
        if response.status_code == 429:
            raise IntegrationError("Límite de la API de búsqueda alcanzado")
        if response.status_code != 200:
            raise IntegrationError(f"Búsqueda web falló ({response.status_code})")
        results = response.json().get("web", {}).get("results", [])
        return [
            SearchResult(
                title=str(r.get("title", ""))[:200],
                url=str(r.get("url", ""))[:500],
                description=str(r.get("description", ""))[:600],
                age=str(r.get("age", "")),
            )
            for r in results[:count]
        ]


_WMO = {
    0: "despejado",
    1: "mayormente despejado",
    2: "parcialmente nuboso",
    3: "nublado",
    45: "niebla",
    48: "niebla con escarcha",
    51: "llovizna ligera",
    53: "llovizna",
    55: "llovizna intensa",
    61: "lluvia ligera",
    63: "lluvia",
    65: "lluvia intensa",
    71: "nieve ligera",
    73: "nieve",
    75: "nieve intensa",
    80: "chubascos ligeros",
    81: "chubascos",
    82: "chubascos fuertes",
    95: "tormenta",
    96: "tormenta con granizo",
    99: "tormenta fuerte con granizo",
}


@dataclass(frozen=True)
class DayForecast:
    day: str
    summary: str
    t_max: float
    t_min: float
    rain_prob: int


class Weather:
    def __init__(self, http: httpx.AsyncClient, latitude: float, longitude: float, tz: str) -> None:
        self._http = http
        self._lat = latitude
        self._lon = longitude
        self._tz = tz

    async def forecast(self, days: int = 3) -> list[DayForecast]:
        try:
            response = await self._http.get(
                OPEN_METEO_URL,
                params={
                    "latitude": self._lat,
                    "longitude": self._lon,
                    "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                    "precipitation_probability_max",
                    "timezone": self._tz,
                    "forecast_days": max(1, min(days, 7)),
                },
            )
        except httpx.HTTPError as exc:
            raise IntegrationError("Clima no disponible (red)") from exc
        if response.status_code != 200:
            raise IntegrationError(f"Clima no disponible ({response.status_code})")
        daily = response.json().get("daily", {})
        out: list[DayForecast] = []
        for idx, day in enumerate(daily.get("time", [])):
            code = int(daily.get("weather_code", [0])[idx] or 0)
            out.append(
                DayForecast(
                    day=day,
                    summary=_WMO.get(code, f"código {code}"),
                    t_max=float(daily.get("temperature_2m_max", [0])[idx] or 0),
                    t_min=float(daily.get("temperature_2m_min", [0])[idx] or 0),
                    rain_prob=int(daily.get("precipitation_probability_max", [0])[idx] or 0),
                )
            )
        return out


class SpeechToText:
    """Google Cloud Speech-to-Text v1 con API key restringida a esa API."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        api_key: str,
        language: str,
        alternatives: list[str],
        poll_interval_s: float = 2.0,
        max_wait_s: float = 300.0,
    ) -> None:
        self._http = http
        self._key = api_key
        self._language = language
        self._alternatives = alternatives[:3]
        self._poll = poll_interval_s
        self._max_wait = max_wait_s

    def _config(self, mime: str) -> dict[str, Any]:
        encoding = "OGG_OPUS" if "ogg" in mime or "opus" in mime else "WEBM_OPUS"
        return {
            "encoding": encoding,
            "sampleRateHertz": 48000,
            "languageCode": self._language,
            "alternativeLanguageCodes": self._alternatives,
            "enableAutomaticPunctuation": True,
        }

    async def transcribe(self, audio: bytes, mime: str = "audio/ogg", duration_s: int = 0) -> str:
        if not self._key:
            raise NotConfiguredError("Falta GOOGLE_STT_API_KEY en el .env")
        if len(audio) > 10 * 1024 * 1024:
            raise IntegrationError("Nota de voz demasiado larga (>10 MB)")
        body = {
            "config": self._config(mime),
            "audio": {"content": base64.b64encode(audio).decode()},
        }
        headers = {"X-Goog-Api-Key": self._key}
        try:
            if duration_s and duration_s > 55:
                response = await self._http.post(STT_ASYNC_URL, json=body, headers=headers)
                self._check(response)
                data = await self._wait_operation(response.json()["name"], headers)
            else:
                response = await self._http.post(STT_SYNC_URL, json=body, headers=headers)
                self._check(response)
                data = response.json()
        except httpx.HTTPError as exc:
            raise IntegrationError(
                f"Error de red en la transcripción: {type(exc).__name__}"
            ) from exc
        parts = [
            r["alternatives"][0].get("transcript", "")
            for r in data.get("results", [])
            if r.get("alternatives")
        ]
        text = " ".join(p.strip() for p in parts if p.strip())
        if not text:
            raise IntegrationError("No se entendió la nota de voz")
        return text

    @staticmethod
    def _check(response: httpx.Response) -> None:
        if response.status_code != 200:
            raise IntegrationError(f"Transcripción falló ({response.status_code})")

    async def _wait_operation(self, name: str, headers: dict[str, str]) -> dict[str, Any]:
        deadline = time.monotonic() + self._max_wait
        while time.monotonic() < deadline:
            response = await self._http.get(STT_OPERATION_URL + name, headers=headers)
            self._check(response)
            data: dict[str, Any] = response.json()
            if data.get("done"):
                if "error" in data:
                    raise IntegrationError("La transcripción devolvió un error")
                result: dict[str, Any] = data.get("response", {})
                return result
            await asyncio.sleep(self._poll)
        raise IntegrationError("La transcripción tardó demasiado")
