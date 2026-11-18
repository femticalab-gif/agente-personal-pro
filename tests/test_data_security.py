"""Cifrado en reposo, backup/restore, rotación de claves, session store, logs y presupuesto."""

from __future__ import annotations

import io
import json
import logging
import tarfile
from pathlib import Path
from typing import Any

import pytest
from claude_agent_sdk.testing import run_session_store_conformance

from marc_agent.agent.session_store import EncryptedSessionStore
from marc_agent.audit import REDACTED, AuditLog, Redactor
from marc_agent.audit.logger import JsonFormatter, setup_logging
from marc_agent.budget import BudgetExceededError, BudgetTracker
from marc_agent.config import Budget, Pricing
from marc_agent.crypto import Cipher, CryptoError, StreamEncryptor, derive_key, generate_key
from marc_agent.db import Database
from marc_agent.llm import LLMUnavailableError
from marc_agent.ops.backup import BackupError, BackupManager
from marc_agent.ops.export import decrypt_export, export_encrypted
from marc_agent.services import Services
from tests.fakes import FakeNotifier

SECRETS_IN_CLEAR = (
    "Sertralina",
    "50 mg",
    "depresión",
    "ES9121000418450200051332",
    "Hipoteca BBVA",
    "1234,56",
    "colesterol 245",
    "Pasaporte",
    "XK123456",
    "Mi secreto personal",
)


def _populate(svc: Services) -> None:
    svc.health.add_treatment("Sertralina", "50 mg", ["08:00"], notes="depresión")
    svc.health.add_lab_result("2026-09-01", "Perfil lipídico", "colesterol 245")
    svc.health.add_appointment("2026-11-03T09:30", "psiquiatría", "Dra. López")
    svc.finance.add_expense(
        1234.56, "vivienda", merchant="Hipoteca BBVA", note="IBAN ES9121000418450200051332"
    )
    svc.finance.add_loan("Hipoteca BBVA", 1234.56, 5, lender="BBVA")
    svc.vault.store(
        b"Pasaporte XK123456 caduca 2030",
        "text/plain",
        "pasaporte.txt",
        title="Pasaporte",
        doc_type="identidad",
        text="Pasaporte XK123456",
    )
    svc.notes.create("Nota", "Mi secreto personal")
    svc.memory.save("Mi secreto personal es la contraseña del wifi", "usuario")


def test_sensitive_data_not_in_clear_on_disk(svc: Services) -> None:
    _populate(svc)
    svc.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    raw = Path(svc.db.path).read_bytes()
    for needle in SECRETS_IN_CLEAR:
        assert needle.encode() not in raw, needle
    for vault_file in svc.config.vault_dir.glob("*.bin"):
        content = vault_file.read_bytes()
        assert b"XK123456" not in content and b"Pasaporte" not in content
    # …y sin embargo se leen bien con la clave.
    assert svc.health.list_treatments()[0]["medicamento"] == "Sertralina"
    data, _, _ = svc.vault.read_file(svc.vault.list_docs()[0]["id"])
    assert b"XK123456" in data
    # La búsqueda de texto completo funciona (índice en memoria).
    assert svc.vault.search("pasaporte")


def test_wrong_key_cannot_read(svc: Services, tmp_path: Path) -> None:
    _populate(svc)
    other = Database(svc.db.path, Cipher(generate_key()))
    row = other.query_one("SELECT payload FROM treatments")
    assert row is not None
    with pytest.raises(CryptoError):
        other.dec(row["payload"])
    other.close()


def test_key_rotation(svc: Services, secrets: Any) -> None:
    _populate(svc)
    new_key = generate_key()
    svc.db.cipher = Cipher(new_key, [secrets.data_key])
    assert svc.db.rotate_encryption() > 5
    only_new = Database(svc.db.path, Cipher(new_key))
    row = only_new.query_one("SELECT payload FROM treatments")
    assert row is not None and only_new.dec(row["payload"])["medicamento"] == "Sertralina"
    only_new.close()


def test_backup_and_restore_roundtrip(svc: Services, secrets: Any) -> None:
    _populate(svc)
    manager = BackupManager(
        svc.config.backup_dir, svc.config.tmp_dir, svc.config.vault_dir, secrets.backup_key, keep=2
    )
    path = manager.create(svc.db)
    assert path.exists() and b"Sertralina" not in path.read_bytes()
    manifest = manager.verify(path)
    assert manifest["format"] == 1 and any(k.startswith("vault/") for k in manifest["members"])
    # Se pierde información…
    svc.db.execute("DELETE FROM treatments")
    for f in svc.config.vault_dir.glob("*.bin"):
        f.unlink()
    svc.db.close()
    # …y se restaura.
    manager.restore(path, svc.config.db_path)
    restored = Database(svc.config.db_path, svc.cipher)
    row = restored.query_one("SELECT payload FROM treatments")
    assert row is not None and restored.dec(row["payload"])["medicamento"] == "Sertralina"
    assert list(svc.config.vault_dir.glob("*.bin"))
    assert svc.config.db_path.with_suffix(".pre-restore").exists()
    restored.close()
    # Retención: se conservan solo ``keep`` backups.
    reopened = Database(svc.config.db_path, svc.cipher)
    for _ in range(3):
        manager.create(reopened)
    assert len(manager.list_backups()) <= 3
    reopened.close()


def test_backup_tampering_and_wrong_key_detected(svc: Services, secrets: Any) -> None:
    manager = BackupManager(
        svc.config.backup_dir, svc.config.tmp_dir, svc.config.vault_dir, secrets.backup_key
    )
    path = manager.create(svc.db)
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    tampered = path.with_name("backup-tampered.tar.enc")
    tampered.write_bytes(bytes(data))
    with pytest.raises(BackupError):
        manager.verify(tampered)
    truncated = path.with_name("backup-trunc.tar.enc")
    truncated.write_bytes(path.read_bytes()[:-10])
    with pytest.raises(BackupError):
        manager.verify(truncated)
    wrong = BackupManager(
        svc.config.backup_dir, svc.config.tmp_dir, svc.config.vault_dir, generate_key()
    )
    with pytest.raises(BackupError):
        wrong.verify(path)


def test_backup_rejects_path_traversal(svc: Services, secrets: Any) -> None:
    manager = BackupManager(
        svc.config.backup_dir, svc.config.tmp_dir, svc.config.vault_dir, secrets.backup_key
    )
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        payload = b"evil"
        info = tarfile.TarInfo("../../etc/evil")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    enc = StreamEncryptor(secrets.backup_key, "backup").encrypt_bytes(buf.getvalue())
    bad = svc.config.backup_dir / "backup-evil.tar.enc"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_bytes(enc)
    with pytest.raises(BackupError):
        manager.verify(bad)


def test_export_roundtrip_and_wrong_key(svc: Services, secrets: Any) -> None:
    _populate(svc)
    data, name = export_encrypted(svc, secrets.export_key)
    assert name.startswith("marc-export-") and b"Sertralina" not in data
    dump = decrypt_export(data, secrets.export_key)
    meds = [t["payload"]["medicamento"] for t in dump["tablas"]["treatments"]]
    assert meds == ["Sertralina"]
    with pytest.raises(CryptoError):
        decrypt_export(data, generate_key())


def test_stream_encryptor_edge_cases(secrets: Any) -> None:
    enc = StreamEncryptor(secrets.backup_key, "x")
    assert enc.decrypt_bytes(enc.encrypt_bytes(b"")) == b""
    big = b"a" * (3 * 1024 * 1024 + 7)
    assert enc.decrypt_bytes(enc.encrypt_bytes(big)) == big
    with pytest.raises(CryptoError):
        enc.decrypt_bytes(b"NOTMAGIC" + b"\x00" * 20)
    with pytest.raises(CryptoError):
        enc.decrypt_bytes(enc.encrypt_bytes(b"data") + b"extra")
    with pytest.raises(CryptoError):
        enc.decrypt_bytes(b"")
    with pytest.raises(CryptoError):
        derive_key("not-base64!!", "p")
    with pytest.raises(CryptoError):
        Cipher("invalid")


def test_cipher_blind_index_is_deterministic(svc: Services) -> None:
    assert svc.cipher.blind_index("Ana@Example.com ") == svc.cipher.blind_index("ana@example.com")
    assert svc.cipher.blind_index("a@b.c") != svc.cipher.blind_index("a@b.d")


async def test_session_store_conformance(svc: Services) -> None:
    counter = {"n": 0}

    def factory() -> EncryptedSessionStore:
        counter["n"] += 1
        db = Database(":memory:", svc.cipher)
        return EncryptedSessionStore(db)

    await run_session_store_conformance(factory)
    assert counter["n"] > 1


async def test_session_store_encrypts_transcripts(svc: Services) -> None:
    store = EncryptedSessionStore(svc.db)
    key = {"project_key": "p", "session_id": "s1"}
    await store.append(key, [{"type": "user", "uuid": "u1", "message": "Mi secreto personal"}])
    await store.append({**key, "subpath": "subagents/agent-1"}, [{"type": "user", "uuid": "u2"}])
    raw = Path(svc.db.path).read_bytes() + Path(str(svc.db.path) + "-wal").read_bytes()
    assert b"Mi secreto personal" not in raw
    assert (await store.load(key))[0]["message"] == "Mi secreto personal"  # type: ignore[index]
    assert await store.list_subkeys(key) == ["subagents/agent-1"]
    assert store.stats()["sessions"] == 1
    store.delete_session_everywhere("s1")
    assert await store.load(key) is None


# -- logs ------------------------------------------------------------------------------
def test_redactor_patterns() -> None:
    redactor = Redactor(["mi-secreto-supersecreto"])
    text = (
        "token 123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA anthropic sk-ant-api03-abcdefghijk "
        "google AIzaSyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA ya29.a0AfH6SM refresh 1//0gAAAAAAAAAAAAAAAAAAAA "
        "Bearer abc.def iban ES91 2100 0418 4502 0005 1332 tarjeta 4111 1111 1111 1111 "
        "secreto mi-secreto-supersecreto tskey-auth-abc123 GOCSPX-abcdef"
    )
    out = redactor.text(text)
    for leaked in (
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        "sk-ant-api03",
        "AIzaSy",
        "ya29.",
        "1//0g",
        "abc.def",
        "2100 0418",
        "4111 1111",
        "mi-secreto-supersecreto",
        "tskey-auth",
        "GOCSPX-abcdef",
    ):
        assert leaked not in out, leaked
    assert redactor.text("pedido 1234 5678") == "pedido 1234 5678"  # sin Luhn válido
    nested = redactor.value(
        {"api_key": "x", "items": [{"password": "y"}, "sk-ant-zzzzzzzzzzzz"], "n": 3}
    )
    assert nested["api_key"] == REDACTED and nested["items"][0]["password"] == REDACTED
    assert nested["items"][1] == REDACTED and nested["n"] == 3
    redactor.add_secrets(["otro-secreto-largo"])
    assert redactor.text("otro-secreto-largo") == REDACTED


def test_json_logs_are_redacted(tmp_path: Path, secrets: Any, capsys: Any) -> None:
    redactor = Redactor(secrets.all_values())
    setup_logging(tmp_path, redactor)
    log = logging.getLogger("test.redaction")
    log.warning("calling https://api.telegram.org/bot%s/getMe", secrets.telegram_bot_token)
    audit = AuditLog(derive_key(secrets.data_key, "audit-digest"))
    audit.tool_call(
        tool="gmail_send",
        category="SENSITIVE",
        decision="ask",
        args={"to": ["a@b.c"], "body": "IBAN ES9121000418450200051332"},
    )
    try:
        raise ValueError(f"fallo con {secrets.anthropic_api_key}")
    except ValueError:
        log.exception("boom")
    for handler in logging.getLogger().handlers:
        handler.flush()
    content = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert secrets.telegram_bot_token not in content
    assert secrets.anthropic_api_key not in content
    assert "ES9121000418450200051332" not in content  # el cuerpo ni siquiera se loguea
    lines = [json.loads(line) for line in content.splitlines()]
    tool = next(entry for entry in lines if entry.get("event") == "tool_call")
    assert tool["arg_keys"] == ["body", "to"] and len(tool["args_digest"]) == 16
    logging.getLogger().handlers.clear()


def test_json_formatter_handles_extra_and_exc() -> None:
    formatter = JsonFormatter(Redactor())
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "msg %s", ("arg",), None)
    record.custom = {"token": "abc"}
    data = json.loads(formatter.format(record))
    assert data["msg"] == "msg arg" and data["custom"]["token"] == REDACTED


# -- presupuesto ---------------------------------------------------------------------------
async def test_budget_warns_at_80_and_blocks_at_100(svc: Services) -> None:
    notifier = FakeNotifier()
    budget = BudgetTracker(
        svc.db,
        Budget(daily_usd=1.0, monthly_usd=100.0, warn_ratio=0.8),
        Pricing(),
        svc.config.tz,
        notifier,
    )
    await budget.record(model="claude-haiku-5-5", kind="test", cost_usd=0.5)
    assert notifier.sent == []
    await budget.record(model="claude-haiku-5-5", kind="test", cost_usd=0.35)
    assert "80%" in notifier.sent[-1].text or "85%" in notifier.sent[-1].text
    await budget.record(model="claude-haiku-5-5", kind="test", cost_usd=0.01)
    assert len(notifier.sent) == 1  # el aviso no se repite
    budget.ensure_available()
    await budget.record(model="claude-haiku-5-5", kind="test", cost_usd=0.2)
    assert "Límite de gasto" in notifier.sent[-1].text
    with pytest.raises(BudgetExceededError):
        budget.ensure_available()
    status = budget.status()
    assert status.blocked and status.remaining == 0 and status.by_kind_month["test"] > 1


def test_budget_cost_estimation(svc: Services) -> None:
    budget = BudgetTracker(svc.db, Budget(), Pricing(), svc.config.tz)
    assert budget.cost_for("claude-opus-5-5", 1_000_000, 0) == pytest.approx(4.0)
    assert budget.cost_for("claude-haiku-5-5", 0, 1_000_000) == pytest.approx(0.5)
    # Modelo desconocido ⇒ precio más alto (conservador).
    assert budget.cost_for("modelo-x", 1_000_000, 0) == pytest.approx(4.0)
    assert budget.cost_for("claude-opus-5-5", 0, 0, cache_read=1_000_000) == pytest.approx(0.4)


async def test_budget_blocks_agent_turns_and_llm(app: Any, svc: Services, agent_state: Any) -> None:
    from marc_agent.agent.runtime import UserInput
    from tests.fakes import CaptureSink

    await svc.budget.record(model="claude-opus-5-5", kind="agent", cost_usd=10.0)
    sink = CaptureSink()
    outcome = await app.runtime.run_turn(UserInput("hola"), sink)
    assert not outcome.ok and sink.failed and "Límite" in sink.final
    assert agent_state.prompts == []


async def test_anthropic_llm_wrapper(svc: Services) -> None:
    import anthropic
    import httpx2

    from marc_agent.llm import AnthropicLLM

    responses = [
        httpx2.Response(
            200,
            json={
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-5-5",
                "content": [
                    {"type": "text", "text": '{"economic": true, "health": false, "reason": "x"}'}
                ],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 10,
                    "cache_read_input_tokens": 50,
                    "cache_creation_input_tokens": 0,
                },
            },
        ),
        httpx2.Response(
            200,
            json={
                "id": "msg_2",
                "type": "message",
                "role": "assistant",
                "model": "claude-haiku-5-5",
                "content": [{"type": "text", "text": "Resumen"}],
                "stop_reason": "refusal",
                "stop_sequence": None,
                "usage": {"input_tokens": 5, "output_tokens": 1},
            },
        ),
        httpx2.Response(
            529, json={"type": "error", "error": {"type": "overloaded_error", "message": "x"}}
        ),
        httpx2.Response(
            429, json={"type": "error", "error": {"type": "rate_limit_error", "message": "x"}}
        ),
    ]
    requests: list[Any] = []

    def handler(request: Any) -> Any:
        requests.append(request)
        return responses.pop(0)

    client = anthropic.AsyncAnthropic(
        api_key="sk-ant-test",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    llm = AnthropicLLM(client, "claude-haiku-5-5", svc.budget)
    data = await llm.json(purpose="t", system="s", content="c", schema={"type": "object"})
    assert data["economic"] is True
    body = json.loads(requests[0].content)
    assert body["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["model"] == "claude-haiku-5-5"
    assert svc.budget.status().month_calls == 1
    with pytest.raises(LLMUnavailableError, match="rechazó"):
        await llm.text(purpose="t", system="s", content="c")
    with pytest.raises(LLMUnavailableError, match="529"):
        await llm.text(purpose="t", system="s", content="c")
    with pytest.raises(LLMUnavailableError, match="Límite"):
        await llm.text(purpose="t", system="s", content="c")
    await svc.budget.record(model="x", kind="t", cost_usd=100)
    with pytest.raises(LLMUnavailableError, match="Límite de gasto"):
        await llm.text(purpose="t", system="s", content="c")
    assert len(requests) == 4  # el presupuesto bloquea antes de llamar a la API
