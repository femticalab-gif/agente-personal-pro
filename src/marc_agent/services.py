"""Contenedor de servicios compartidos (inyección de dependencias manual)."""

from __future__ import annotations

from dataclasses import dataclass, field

from marc_agent.audit import AuditLog
from marc_agent.budget import BudgetTracker
from marc_agent.config import AppConfig
from marc_agent.crypto import Cipher
from marc_agent.db import Database, SearchIndex
from marc_agent.domain.finance import FinanceService
from marc_agent.domain.health import HealthService
from marc_agent.domain.mail import MailService
from marc_agent.domain.memory import MemoryService
from marc_agent.domain.notes_tasks import ContactsService, NotesService, TasksService
from marc_agent.domain.profile import ChatLog, ProfileService
from marc_agent.domain.reminders import RemindersService
from marc_agent.domain.vault import VaultService
from marc_agent.integrations.google import CalendarClient, DriveClient, GmailClient, GoogleAuth
from marc_agent.integrations.web import BraveSearch, SpeechToText, Weather
from marc_agent.llm import LLM
from marc_agent.notify import Keyboard, Notifier, NullNotifier
from marc_agent.permissions.state import ControlState
from marc_agent.security.classifier import OutboundClassifier
from marc_agent.security.fraud import FraudDetector, FraudRegistry
from marc_agent.security.taint import TurnTracker
from marc_agent.security.untrusted import UntrustedWrapper


class NotifierProxy:
    """Delegado mutable: los servicios se crean antes de que arranque el bot."""

    def __init__(self, target: Notifier | None = None) -> None:
        self.target: Notifier = target or NullNotifier()

    async def send(self, text: str, buttons: Keyboard | None = None) -> int | None:
        return await self.target.send(text, buttons)

    async def edit(self, message_id: int, text: str, buttons: Keyboard | None = None) -> None:
        await self.target.edit(message_id, text, buttons)

    async def send_document(self, data: bytes, filename: str, caption: str = "") -> None:
        await self.target.send_document(data, filename, caption)


@dataclass
class Services:
    config: AppConfig
    db: Database
    index: SearchIndex
    cipher: Cipher
    audit: AuditLog
    budget: BudgetTracker
    llm: LLM | None
    wrapper: UntrustedWrapper
    turns: TurnTracker
    state: ControlState
    classifier: OutboundClassifier
    fraud_detector: FraudDetector
    fraud_registry: FraudRegistry
    notifier: NotifierProxy
    google_auth: GoogleAuth
    gmail: GmailClient
    calendar: CalendarClient
    drive: DriveClient
    mail: MailService
    search: BraveSearch
    weather: Weather
    stt: SpeechToText
    memory: MemoryService
    notes: NotesService
    tasks: TasksService
    contacts: ContactsService
    finance: FinanceService
    health: HealthService
    reminders: RemindersService
    vault: VaultService
    profile: ProfileService
    chatlog: ChatLog
    extras: dict[str, object] = field(default_factory=dict)

    def rebuild_indexes(self) -> None:
        self.memory.rebuild_index()
        self.notes.rebuild_index()
        self.contacts.rebuild_index()
        self.vault.rebuild_index()
