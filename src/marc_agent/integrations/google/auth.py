"""OAuth 2.0 de Google con scopes mínimos y tokens cifrados en reposo.

Scopes:
* ``gmail.modify``: leer, etiquetar/archivar, borradores y envío. NO permite el borrado
  permanente. Es el mínimo que cubre archivar (``messages.modify``).
* ``calendar.events``: eventos del calendario (sin gestionar calendarios ni ACL).
* ``drive.readonly``: buscar y leer documentos. Sin escritura ni compartición.

Flujo inicial (``marc-agent google-auth``): app de escritorio con PKCE y redirección
loopback. Marc abre la URL en su navegador, autoriza, y pega en su terminal la URL a la
que le redirige el navegador. Ningún secreto pasa por el chat.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from marc_agent.db import Database
from marc_agent.integrations import IntegrationError, NotConfiguredError
from marc_agent.util import iso, utcnow

SCOPES = (
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/drive.readonly",
)
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - URL pública, no un secreto
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
REDIRECT_URI = "http://127.0.0.1:8765/"
PROVIDER = "google"


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class GoogleAuth:
    def __init__(
        self, db: Database, http: httpx.AsyncClient, client_id: str, client_secret: str
    ) -> None:
        self._db = db
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._cached: dict[str, Any] | None = None

    @property
    def configured(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def _load(self) -> dict[str, Any] | None:
        if self._cached is not None:
            return self._cached
        row = self._db.query_one("SELECT payload FROM oauth_tokens WHERE provider=?", (PROVIDER,))
        if row is None:
            return None
        data: dict[str, Any] = self._db.dec(row["payload"])
        self._cached = data
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self._db.execute(
            "INSERT INTO oauth_tokens(provider, payload, updated_at) VALUES(?,?,?) "
            "ON CONFLICT(provider) DO UPDATE SET payload=excluded.payload, "
            "updated_at=excluded.updated_at",
            (PROVIDER, self._db.enc(data), iso(utcnow())),
        )
        self._cached = data

    def has_token(self) -> bool:
        data = self._load()
        return bool(data and data.get("refresh_token"))

    def granted_scopes(self) -> list[str]:
        data = self._load() or {}
        return str(data.get("scope", "")).split()

    def build_auth_url(self, state: str, code_challenge: str) -> str:
        if not self.configured:
            raise NotConfiguredError("Faltan GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET en el .env")
        params = {
            "client_id": self._client_id,
            "redirect_uri": REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "false",
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    @staticmethod
    def parse_redirect(redirect_url: str, expected_state: str) -> str:
        query = parse_qs(urlparse(redirect_url.strip()).query)
        if query.get("error"):
            raise IntegrationError(f"Google devolvió un error: {query['error'][0]}")
        if query.get("state", [""])[0] != expected_state:
            raise IntegrationError(
                "El parámetro state no coincide (posible CSRF). Repite el proceso."
            )
        code = query.get("code", [""])[0]
        if not code:
            raise IntegrationError("La URL no contiene el código de autorización")
        return code

    async def exchange_code(self, code: str, code_verifier: str) -> None:
        response = await self._http.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": REDIRECT_URI,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            },
        )
        if response.status_code != 200:
            raise IntegrationError(f"No se pudo canjear el código ({response.status_code})")
        payload = response.json()
        if not payload.get("refresh_token"):
            raise IntegrationError("Google no devolvió refresh_token. Revoca el acceso y repite.")
        self._save(
            {
                "access_token": payload["access_token"],
                "refresh_token": payload["refresh_token"],
                "expires_at": time.time() + int(payload.get("expires_in", 3600)),
                "scope": payload.get("scope", ""),
            }
        )

    async def access_token(self, force_refresh: bool = False) -> str:
        data = self._load()
        if not data or not data.get("refresh_token"):
            raise NotConfiguredError(
                "Google no está autorizado. Ejecuta en el servidor: "
                "make google-auth (docker compose run --rm agent google-auth)"
            )
        if not force_refresh and float(data.get("expires_at", 0)) - 60 > time.time():
            return str(data["access_token"])
        response = await self._http.post(
            TOKEN_URL,
            data={
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "refresh_token": data["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
        if response.status_code != 200:
            raise IntegrationError(
                f"No se pudo renovar el token de Google ({response.status_code}). Si persiste, "
                "vuelve a autorizar con marc-agent google-auth."
            )
        payload = response.json()
        data = {
            **data,
            "access_token": payload["access_token"],
            "expires_at": time.time() + int(payload.get("expires_in", 3600)),
        }
        if payload.get("refresh_token"):
            data["refresh_token"] = payload["refresh_token"]
        self._save(data)
        return str(data["access_token"])

    async def revoke(self) -> None:
        data = self._load()
        if not data:
            return
        token = data.get("refresh_token") or data.get("access_token")
        if token:
            await self._http.post(REVOKE_URL, data={"token": token})
        self._db.execute("DELETE FROM oauth_tokens WHERE provider=?", (PROVIDER,))
        self._cached = None


class GoogleApi:
    """Cliente HTTP autenticado con reintento único tras renovar el token."""

    def __init__(self, auth: GoogleAuth, http: httpx.AsyncClient) -> None:
        self.auth = auth
        self._http = http

    async def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        extra_headers: dict[str, str] = kwargs.pop("headers", {})
        for attempt in range(2):
            token = await self.auth.access_token(force_refresh=attempt == 1)
            headers = {**extra_headers, "Authorization": f"Bearer {token}"}
            try:
                response = await self._http.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                raise IntegrationError(f"Error de red con Google: {type(exc).__name__}") from exc
            if response.status_code == 401 and attempt == 0:
                continue
            if response.status_code >= 400:
                raise IntegrationError(_google_error(response))
            return response
        raise IntegrationError("Google rechazó las credenciales")

    async def json(self, method: str, url: str, **kwargs: Any) -> Any:
        response = await self.request(method, url, **kwargs)
        if not response.content:
            return {}
        return response.json()


def _google_error(response: httpx.Response) -> str:
    try:
        message = response.json().get("error", {}).get("message", "")
    except ValueError:
        message = ""
    return f"Google API {response.status_code}: {message[:300]}".strip()
