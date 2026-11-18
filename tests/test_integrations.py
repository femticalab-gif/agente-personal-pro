"""Integraciones externas con respx: OAuth de Google, Gmail/Calendar/Drive, documentos, web."""

from __future__ import annotations

import io
import json
import time
from datetime import datetime
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import httpx
import pytest
import respx

from marc_agent.integrations import IntegrationError, NotConfiguredError
from marc_agent.integrations.docparse import DOCX, PDF, XLSX, extract_text
from marc_agent.integrations.google.auth import (
    REVOKE_URL,
    SCOPES,
    TOKEN_URL,
    GoogleApi,
    GoogleAuth,
    pkce_pair,
)
from marc_agent.integrations.google.calendar import (
    CalEvent,
    event_body,
    find_conflicts,
    parse_event,
)
from marc_agent.integrations.google.gmail import GmailClient, extract_body, split_addresses
from marc_agent.integrations.web import BraveSearch, SpeechToText, Weather
from marc_agent.services import Services

TZ = ZoneInfo("Europe/Madrid")


# -- OAuth -------------------------------------------------------------------------------
def test_pkce_and_auth_url(svc: Services) -> None:
    verifier, challenge = pkce_pair()
    assert 43 <= len(verifier) <= 128 and "=" not in challenge
    url = svc.google_auth.build_auth_url("st4te", challenge)
    query = parse_qs(urlparse(url).query)
    assert query["scope"][0].split() == list(SCOPES)
    assert query["access_type"] == ["offline"] and query["code_challenge_method"] == ["S256"]
    assert query["redirect_uri"] == ["http://127.0.0.1:8765/"]
    assert (
        GoogleAuth.parse_redirect("http://127.0.0.1:8765/?state=st4te&code=abc", "st4te") == "abc"
    )
    with pytest.raises(IntegrationError, match="CSRF"):
        GoogleAuth.parse_redirect("http://127.0.0.1:8765/?state=otro&code=abc", "st4te")
    with pytest.raises(IntegrationError, match="error"):
        GoogleAuth.parse_redirect("http://127.0.0.1:8765/?error=access_denied", "st4te")
    with pytest.raises(IntegrationError, match="código"):
        GoogleAuth.parse_redirect("http://127.0.0.1:8765/?state=st4te", "st4te")


def test_auth_url_requires_client(svc: Services, http: httpx.AsyncClient) -> None:
    auth = GoogleAuth(svc.db, http, "", "")
    assert not auth.configured
    with pytest.raises(NotConfiguredError):
        auth.build_auth_url("s", "c")


async def test_exchange_refresh_revoke(svc: Services, mock_http: respx.MockRouter) -> None:
    auth = svc.google_auth
    token_route = mock_http.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "access_token": "ya29.first",
                    "refresh_token": "1//refresh-token-value",
                    "expires_in": 1,
                    "scope": " ".join(SCOPES),
                },
            ),
            httpx.Response(200, json={"access_token": "ya29.second", "expires_in": 3600}),
            httpx.Response(400, json={"error": "invalid_grant"}),
        ]
    )
    await auth.exchange_code("code-1", "verifier-1")
    body = parse_qs(token_route.calls[0].request.content.decode())
    assert body["grant_type"] == ["authorization_code"] and body["code_verifier"] == ["verifier-1"]
    assert auth.has_token() and auth.granted_scopes() == list(SCOPES)
    raw = svc.db.query_one("SELECT payload FROM oauth_tokens")
    assert raw is not None and b"refresh-token-value" not in raw["payload"]  # cifrado en reposo
    assert await auth.access_token() == "ya29.second"  # caducado ⇒ refresco
    assert await auth.access_token() == "ya29.second"  # cacheado
    with pytest.raises(IntegrationError, match="renovar"):
        await auth.access_token(force_refresh=True)
    revoke = mock_http.post(REVOKE_URL).respond(200)
    await auth.revoke()
    assert revoke.called and not auth.has_token()
    with pytest.raises(NotConfiguredError):
        await auth.access_token()


async def test_exchange_errors(svc: Services, mock_http: respx.MockRouter) -> None:
    mock_http.post(TOKEN_URL).mock(
        side_effect=[
            httpx.Response(400, json={}),
            httpx.Response(200, json={"access_token": "a", "expires_in": 3600}),
        ]
    )
    with pytest.raises(IntegrationError, match="canjear"):
        await svc.google_auth.exchange_code("c", "v")
    with pytest.raises(IntegrationError, match="refresh_token"):
        await svc.google_auth.exchange_code("c", "v")


async def test_google_api_retries_once_on_401(svc: Services, mock_http: respx.MockRouter) -> None:
    svc.db.execute(
        "INSERT INTO oauth_tokens(provider, payload, updated_at) VALUES('google', ?, 'x')",
        (
            svc.db.enc(
                {"access_token": "old", "refresh_token": "1//r", "expires_at": time.time() + 3600}
            ),
        ),
    )
    mock_http.post(TOKEN_URL).respond(200, json={"access_token": "new", "expires_in": 3600})
    route = mock_http.get("https://www.googleapis.com/x").mock(
        side_effect=[httpx.Response(401), httpx.Response(200, json={"ok": True})]
    )
    api = GoogleApi(svc.google_auth, svc.extras["http"])  # type: ignore[arg-type]
    assert await api.json("GET", "https://www.googleapis.com/x") == {"ok": True}
    assert route.calls[1].request.headers["Authorization"] == "Bearer new"
    mock_http.get("https://www.googleapis.com/y").respond(
        403, json={"error": {"message": "Forbidden"}}
    )
    with pytest.raises(IntegrationError, match="403"):
        await api.json("GET", "https://www.googleapis.com/y")
    mock_http.get("https://www.googleapis.com/z").respond(500, text="no json")
    with pytest.raises(IntegrationError, match="500"):
        await api.json("GET", "https://www.googleapis.com/z")
    mock_http.get("https://www.googleapis.com/empty").respond(204)
    assert await api.json("GET", "https://www.googleapis.com/empty") == {}


# -- Gmail / Calendar helpers --------------------------------------------------------------
def test_gmail_body_and_mime() -> None:
    payload = {
        "parts": [
            {"mimeType": "text/html", "body": {"data": "PGI-SG9sYTwvYj4"}},
            {"mimeType": "application/pdf", "filename": "a.pdf", "body": {"data": "eA"}},
        ]
    }
    text, hidden = extract_body(payload)
    assert text == "Hola" and not hidden
    raw = GmailClient.build_mime(["a@b.com"], "Asunto", "Cuerpo", cc=["c@d.com"])
    assert raw
    assert split_addresses("Ana <ANA@x.com>, b@y.com") == ["ana@x.com", "b@y.com"]
    assert split_addresses(["no válido"]) == []


def test_calendar_helpers() -> None:
    event = parse_event(
        {"id": "1", "start": {"date": "2026-10-20"}, "end": {"date": "2026-10-21"}}, TZ
    )
    assert event.all_day and event.summary == "(sin título)"
    body = event_body(
        "x",
        datetime(2026, 10, 20, tzinfo=TZ),
        datetime(2026, 10, 21, tzinfo=TZ),
        TZ,
        all_day=True,
        attendees=["a@b.c"],
        description="d",
        location="l",
    )
    assert body["start"] == {"date": "2026-10-20"} and body["attendees"] == [{"email": "a@b.c"}]
    a = CalEvent(
        "a",
        "A",
        datetime(2026, 10, 20, 10, tzinfo=TZ),
        datetime(2026, 10, 20, 11, tzinfo=TZ),
        False,
    )
    b = CalEvent(
        "b", "B", datetime(2026, 10, 20, tzinfo=TZ), datetime(2026, 10, 21, tzinfo=TZ), True
    )
    assert find_conflicts(
        [a, b], datetime(2026, 10, 20, 10, 30, tzinfo=TZ), datetime(2026, 10, 20, 12, tzinfo=TZ)
    ) == [a]


# -- documentos ----------------------------------------------------------------------------
def _minimal_pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 712 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    )
    return out.getvalue()


def test_extract_text_formats() -> None:
    assert "Factura 42" in extract_text(_minimal_pdf("Factura 42"), PDF)
    import docx
    from openpyxl import Workbook

    document = docx.Document()
    document.add_paragraph("Contrato de alquiler")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Renta"
    table.cell(0, 1).text = "900"
    buf = io.BytesIO()
    document.save(buf)
    text = extract_text(buf.getvalue(), DOCX)
    assert "Contrato de alquiler" in text and "Renta | 900" in text
    wb = Workbook()
    ws = wb.active
    assert ws is not None
    ws.append(["Mes", "Gasto"])
    ws.append(["Octubre", 120])
    buf = io.BytesIO()
    wb.save(buf)
    assert "Octubre,120" in extract_text(buf.getvalue(), XLSX)
    assert extract_text(b"hola", "text/plain") == "hola"
    with pytest.raises(IntegrationError, match="no soportado"):
        extract_text(b"x", "application/zip")
    with pytest.raises(IntegrationError, match="No se pudo leer"):
        extract_text(b"not a docx", DOCX)
    with pytest.raises(IntegrationError, match="grande"):
        extract_text(b"x" * (21 * 1024 * 1024), "text/plain")


async def test_drive_export_and_errors(svc: Services, mock_http: respx.MockRouter) -> None:
    from tests.conftest import authorize_google

    authorize_google(svc)
    base = "https://www.googleapis.com/drive/v3/files"
    mock_http.get(f"{base}/g1/export").respond(200, text="a,b\n1,2")
    mock_http.get(f"{base}/g1").respond(
        200,
        json={"id": "g1", "name": "Hoja", "mimeType": "application/vnd.google-apps.spreadsheet"},
    )
    mock_http.get(f"{base}/g2").respond(
        200, json={"id": "g2", "name": "Form", "mimeType": "application/vnd.google-apps.form"}
    )
    mock_http.get(f"{base}/g3").respond(
        200,
        json={
            "id": "g3",
            "name": "Big",
            "mimeType": "application/pdf",
            "size": str(30 * 1024 * 1024),
        },
    )
    info, text = await svc.drive.read_text("g1")
    assert info.name == "Hoja" and "1,2" in text
    with pytest.raises(IntegrationError, match="no exportable"):
        await svc.drive.read_text("g2")
    with pytest.raises(IntegrationError, match="grande"):
        await svc.drive.read_text("g3")


# -- web, clima, STT -----------------------------------------------------------------------
async def test_brave_errors(mock_http: respx.MockRouter, http: httpx.AsyncClient) -> None:
    with pytest.raises(NotConfiguredError):
        await BraveSearch(http, "").search("x")
    url = "https://api.search.brave.com/res/v1/web/search"
    mock_http.get(url).mock(
        side_effect=[httpx.Response(429), httpx.Response(500), httpx.ConnectError("x")]
    )
    brave = BraveSearch(http, "k")
    for expected in ("Límite", "falló", "red"):
        with pytest.raises(IntegrationError, match=expected):
            await brave.search("x")


async def test_weather_errors(mock_http: respx.MockRouter, http: httpx.AsyncClient) -> None:
    url = "https://api.open-meteo.com/v1/forecast"
    mock_http.get(url).mock(
        side_effect=[
            httpx.Response(500),
            httpx.ConnectError("x"),
            httpx.Response(200, json={"daily": {"time": ["d"], "weather_code": [7]}}),
        ]
    )
    weather = Weather(http, 1, 2, "Europe/Madrid")
    with pytest.raises(IntegrationError):
        await weather.forecast()
    with pytest.raises(IntegrationError):
        await weather.forecast()
    days = await weather.forecast()
    assert days[0].summary == "código 7"


async def test_stt_errors(mock_http: respx.MockRouter, http: httpx.AsyncClient) -> None:
    with pytest.raises(NotConfiguredError):
        await SpeechToText(http, "", "es-ES", []).transcribe(b"x")
    stt = SpeechToText(
        http,
        "key",
        "es-ES",
        ["ca-ES", "en-US", "fr-FR", "de-DE"],
        poll_interval_s=0,
        max_wait_s=0.01,
    )
    with pytest.raises(IntegrationError, match="larga"):
        await stt.transcribe(b"x" * (11 * 1024 * 1024))
    route = mock_http.post("https://speech.googleapis.com/v1/speech:recognize").mock(
        side_effect=[httpx.Response(200, json={"results": []}), httpx.ConnectError("x")]
    )
    with pytest.raises(IntegrationError, match="entendió"):
        await stt.transcribe(b"x", "audio/webm")
    config = json.loads(route.calls[0].request.content)["config"]
    assert config["encoding"] == "WEBM_OPUS" and len(config["alternativeLanguageCodes"]) == 3
    with pytest.raises(IntegrationError, match="red"):
        await stt.transcribe(b"x")
    mock_http.post("https://speech.googleapis.com/v1/speech:longrunningrecognize").respond(
        200, json={"name": "o"}
    )
    polls = {"n": 0}

    def operation(_: httpx.Request) -> httpx.Response:
        polls["n"] += 1
        if polls["n"] == 1:
            return httpx.Response(200, json={"done": True, "error": {"code": 3}})
        return httpx.Response(200, json={"done": False})

    mock_http.get("https://speech.googleapis.com/v1/operations/o").mock(side_effect=operation)
    with pytest.raises(IntegrationError, match="error"):
        await stt.transcribe(b"x", duration_s=120)
    with pytest.raises(IntegrationError, match="demasiado"):
        await stt.transcribe(b"x", duration_s=120)
