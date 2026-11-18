"""Construcción y arranque de la aplicación."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from dataclasses import dataclass
from typing import Any

import anthropic
import httpx

from marc_agent.agent.runtime import AgentRuntime, ClientFactory, default_client_factory
from marc_agent.agent.session_store import EncryptedSessionStore
from marc_agent.audit import AuditLog, Redactor, setup_logging
from marc_agent.budget import BudgetTracker
from marc_agent.config import AppConfig, Secrets
from marc_agent.crypto import Cipher, derive_key
from marc_agent.db import Database, SearchIndex
from marc_agent.domain.finance import FinanceService
from marc_agent.domain.health import HealthService
from marc_agent.domain.mail import MailService
from marc_agent.domain.memory import MemoryService
from marc_agent.domain.notes_tasks import ContactsService, NotesService, TasksService
from marc_agent.domain.profile import ChatLog, ProfileService
from marc_agent.domain.reminders import RemindersService
from marc_agent.domain.vault import VaultService
from marc_agent.gateway.media import MediaProcessor
from marc_agent.integrations import make_http_client
from marc_agent.integrations.google import (
    CalendarClient,
    DriveClient,
    GmailClient,
    GoogleApi,
    GoogleAuth,
)
from marc_agent.integrations.web import BraveSearch, SpeechToText, Weather
from marc_agent.llm import LLM, AnthropicLLM
from marc_agent.ops.backup import BackupManager
from marc_agent.permissions.approvals import ApprovalBroker, TicketBook
from marc_agent.permissions.engine import PolicyEngine
from marc_agent.permissions.gate import Gatekeeper, ToolExecutor
from marc_agent.permissions.state import ControlState
from marc_agent.scheduler.briefing import BriefingBuilder
from marc_agent.scheduler.jobs import AgentScheduler, JobRunner
from marc_agent.security.classifier import OutboundClassifier
from marc_agent.security.fraud import FraudDetector, FraudRegistry
from marc_agent.security.taint import TurnTracker
from marc_agent.security.untrusted import UntrustedWrapper
from marc_agent.services import NotifierProxy, Services
from marc_agent.tools.catalog import build_registry
from marc_agent.tools.registry import ToolRegistry

log = logging.getLogger(__name__)


def build_services(
    config: AppConfig,
    secrets: Secrets,
    *,
    http: httpx.AsyncClient | None = None,
    llm: LLM | None = None,
    anthropic_client: anthropic.AsyncAnthropic | None = None,
    db: Database | None = None,
) -> Services:
    for directory in (
        config.data_dir,
        config.vault_dir,
        config.backup_dir,
        config.tmp_dir,
        config.agent_cwd,
        config.log_dir,
    ):
        directory.mkdir(parents=True, exist_ok=True)
    cipher = Cipher(secrets.data_key, secrets.data_key_previous)
    db = db or Database(config.db_path, cipher)
    index = SearchIndex()
    audit = AuditLog(derive_key(secrets.data_key, "audit-digest"))
    notifier = NotifierProxy()
    tz = config.tz
    budget = BudgetTracker(db, config.budget, config.pricing, tz, notifier)
    http = http or make_http_client(config.network.effective_proxy)
    if llm is None and secrets.anthropic_api_key:
        client = anthropic_client or anthropic.AsyncAnthropic(
            api_key=secrets.anthropic_api_key,
            http_client=anthropic.DefaultAsyncHttpxClient(proxy=config.network.effective_proxy)
            if config.network.effective_proxy
            else None,
            max_retries=2,
            timeout=120.0,
        )
        llm = AnthropicLLM(client, config.models.cheap, budget)
    wrapper = UntrustedWrapper(owner_name=config.profile.name)
    google_auth = GoogleAuth(db, http, secrets.google_client_id, secrets.google_client_secret)
    google_api = GoogleApi(google_auth, http)
    gmail = GmailClient(google_api)
    fraud_detector = FraudDetector(llm)
    fraud_registry = FraudRegistry(db)
    svc = Services(
        config=config,
        db=db,
        index=index,
        cipher=cipher,
        audit=audit,
        budget=budget,
        llm=llm,
        wrapper=wrapper,
        turns=TurnTracker(),
        state=ControlState(db, config.security.dry_run_default),
        classifier=OutboundClassifier(llm),
        fraud_detector=fraud_detector,
        fraud_registry=fraud_registry,
        notifier=notifier,
        google_auth=google_auth,
        gmail=gmail,
        calendar=CalendarClient(google_api, tz),
        drive=DriveClient(google_api),
        mail=MailService(db, gmail, llm, fraud_detector, fraud_registry, wrapper),
        search=BraveSearch(http, secrets.brave_api_key),
        weather=Weather(
            http, config.profile.latitude, config.profile.longitude, config.profile.timezone
        ),
        stt=SpeechToText(
            http, secrets.google_stt_api_key, config.stt.language, config.stt.alternative_languages
        ),
        memory=MemoryService(db, index, config.profile.name),
        notes=NotesService(db, index),
        tasks=TasksService(db, tz),
        contacts=ContactsService(db, index),
        finance=FinanceService(db, tz),
        health=HealthService(db, tz),
        reminders=RemindersService(db, tz),
        vault=VaultService(db, index, cipher, config.vault_dir),
        profile=ProfileService(db),
        chatlog=ChatLog(db),
    )
    svc.extras["http"] = http
    svc.rebuild_indexes()
    return svc


@dataclass
class App:
    svc: Services
    registry: ToolRegistry
    gate: Gatekeeper
    executor: ToolExecutor
    runtime: AgentRuntime
    store: EncryptedSessionStore
    backups: BackupManager
    briefing: BriefingBuilder
    jobs: JobRunner
    media: MediaProcessor


def build_app(
    config: AppConfig,
    secrets: Secrets,
    *,
    svc: Services | None = None,
    client_factory: ClientFactory = default_client_factory,
    **service_kwargs: Any,
) -> App:
    svc = svc or build_services(config, secrets, **service_kwargs)
    registry = build_registry(svc)
    from marc_agent.agent.runtime import SUBAGENTS

    engine = PolicyEngine(registry, svc.state, svc.classifier, frozenset(SUBAGENTS))
    approvals = ApprovalBroker(svc.db, svc.notifier, config.security.approval_timeout_s)
    gate = Gatekeeper(
        engine, approvals, TicketBook(), svc.state, svc.turns, svc.notifier, svc.audit
    )
    executor = ToolExecutor(gate, registry)
    store = EncryptedSessionStore(svc.db)
    runtime = AgentRuntime(svc, gate, executor, registry, store, client_factory)
    backups = BackupManager(
        config.backup_dir,
        config.tmp_dir,
        config.vault_dir,
        secrets.backup_key,
        config.schedule.backup_keep,
    )
    briefing = BriefingBuilder(svc)
    jobs = JobRunner(svc, briefing, backups, runtime.maybe_close_idle)
    return App(
        svc, registry, gate, executor, runtime, store, backups, briefing, jobs, MediaProcessor(svc)
    )


async def run(
    config: AppConfig,
    secrets: Secrets,
    *,
    stop_event: asyncio.Event | None = None,
    telegram_factory: Any = None,
    app: App | None = None,
) -> None:
    """Arranca bot (long polling) + scheduler y espera a SIGTERM/SIGINT."""
    from marc_agent.gateway.api import TelegramNotifier
    from marc_agent.gateway.core import BOT_COMMANDS, GatewayCore
    from marc_agent.gateway.guard import AccessGuard
    from marc_agent.gateway.telegram_app import TelegramRunner

    redactor = Redactor(secrets.all_values())
    setup_logging(config.log_dir, redactor)
    app = app or build_app(config, secrets)
    svc = app.svc
    factory = telegram_factory or TelegramRunner
    telegram = factory(secrets.telegram_bot_token, config.network.effective_proxy)
    svc.notifier.target = TelegramNotifier(telegram.api, secrets.telegram_user_id)
    scheduler = AgentScheduler(svc, app.jobs)
    guard = AccessGuard(
        secrets.telegram_user_id,
        config.telegram.rate_limit_per_minute,
        config.telegram.rate_limit_burst,
        svc.audit,
    )
    core = GatewayCore(
        svc,
        telegram.api,
        guard,
        app.runtime,
        app.gate,
        app.media,
        app.briefing,
        app.backups,
        secrets.export_key,
        status_extra=scheduler.next_runs,
    )
    telegram.on_update(core.handle)

    stop_event = stop_event or asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)

    await app.jobs.heartbeat_job()
    core.start()
    scheduler.start()
    await telegram.start(BOT_COMMANDS)
    svc.audit.event("started", tools=len(app.registry))
    try:
        await svc.notifier.send("🟢 Agente en marcha.")
    except Exception:
        log.warning("startup_notify_failed")
    try:
        await stop_event.wait()
    finally:
        svc.audit.event("stopping")
        scheduler.shutdown()
        await telegram.stop()
        await core.stop()
        await app.runtime.shutdown()
        http = svc.extras.get("http")
        if isinstance(http, httpx.AsyncClient):
            await http.aclose()
        svc.db.close()
