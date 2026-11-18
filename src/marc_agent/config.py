"""Configuración de la aplicación.

Dos fuentes separadas a propósito:

* ``config.yaml``: preferencias no secretas (perfil, horarios, límites, modelos).
* Variables de entorno (``.env`` en el servidor): secretos. Nunca se loguean ni se
  serializan; ``Secrets.__repr__`` está redactado.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class ConfigError(RuntimeError):
    """Configuración inválida o incompleta."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FamilyMember(_Strict):
    name: str
    relation: str


class Profile(_Strict):
    name: str = "Marc"
    city: str = "Barcelona"
    timezone: str = "Europe/Madrid"
    latitude: float = 41.3874
    longitude: float = 2.1686
    languages: list[str] = Field(default_factory=lambda: ["es", "ca"])
    family: list[FamilyMember] = Field(default_factory=list)
    notes: str = ""
    # Dirección de correo propia: para no tratarla como "tercero" en filtros internos.
    own_email: str = ""

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Zona horaria desconocida: {value}") from exc
        return value


class Models(_Strict):
    main: str = "claude-opus-5-5"
    cheap: str = "claude-haiku-5-5"
    vision: str = "claude-opus-5-5"
    subagent: str = "claude-haiku-5-5"
    fallback: str | None = "claude-sonnet-5-5"
    effort: str = "medium"


class Pricing(_Strict):
    """USD por millón de tokens (entrada, salida). Estimación para el presupuesto."""

    table: dict[str, tuple[float, float]] = Field(
        default_factory=lambda: {
            "claude-opus-5-5": (4.0, 20.0),
            "claude-sonnet-5-5": (2.0, 10.0),
            "claude-haiku-5-5": (0.10, 0.50),
        }
    )
    cache_read_factor: float = 0.1
    cache_write_factor: float = 1.25


class Budget(_Strict):
    daily_usd: float = 5.0
    monthly_usd: float = 60.0
    warn_ratio: float = 0.8


class Schedule(_Strict):
    briefing_time: time = time(7, 45)
    mail_watch_minutes: int = 5
    weekly_day: str = "sun"
    weekly_time: time = time(19, 0)
    backup_time: time = time(3, 30)
    backup_keep: int = 14
    due_reminder_days: list[int] = Field(default_factory=lambda: [7, 1])
    due_reminder_time: time = time(9, 0)
    doc_expiry_days: list[int] = Field(default_factory=lambda: [30, 7])
    appointment_reminders_hours: list[int] = Field(default_factory=lambda: [24, 2])
    session_idle_minutes: int = 30
    heartbeat_seconds: int = 60
    watchdog_stale_minutes: int = 15


class Telegram(_Strict):
    rate_limit_per_minute: int = 20
    rate_limit_burst: int = 8
    stream_edit_interval_s: float = 1.2
    progress_after_s: float = 15.0


class Security(_Strict):
    approval_timeout_s: int = 600
    dry_run_default: bool = False
    max_subagents: int = 4


class Network(_Strict):
    proxy_url: str | None = None

    @property
    def effective_proxy(self) -> str | None:
        """Proxy de egress: el de config.yaml o, si no hay, ``HTTPS_PROXY`` del entorno."""
        return self.proxy_url or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")


class Stt(_Strict):
    language: str = "es-ES"
    alternative_languages: list[str] = Field(default_factory=lambda: ["ca-ES"])


class AppConfig(_Strict):
    profile: Profile = Field(default_factory=Profile)
    models: Models = Field(default_factory=Models)
    pricing: Pricing = Field(default_factory=Pricing)
    budget: Budget = Field(default_factory=Budget)
    schedule: Schedule = Field(default_factory=Schedule)
    telegram: Telegram = Field(default_factory=Telegram)
    security: Security = Field(default_factory=Security)
    network: Network = Field(default_factory=Network)
    stt: Stt = Field(default_factory=Stt)
    data_dir: Path = Path("/data")
    weather_enabled: bool = True

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.profile.timezone)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "db" / "marc.sqlite3"

    @property
    def vault_dir(self) -> Path:
        return self.data_dir / "vault"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def heartbeat_file(self) -> Path:
        return self.data_dir / "heartbeat"

    @property
    def agent_cwd(self) -> Path:
        return self.data_dir / "agent-cwd"


def load_config(path: Path | str | None) -> AppConfig:
    """Carga ``config.yaml``. Sin ruta (o fichero inexistente) usa los valores por defecto."""
    if path is None:
        return AppConfig()
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"No existe el fichero de configuración: {p}")
    raw: Any = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml debe ser un mapa YAML")
    try:
        return AppConfig.model_validate(raw)
    except ValueError as exc:  # pydantic.ValidationError hereda de ValueError
        raise ConfigError(f"config.yaml inválido: {exc}") from exc


_REQUIRED_SECRETS = (
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_USER_ID",
    "ANTHROPIC_API_KEY",
    "MARC_DATA_KEY",
    "MARC_BACKUP_KEY",
    "MARC_EXPORT_KEY",
)


@dataclass(frozen=True, repr=False)
class Secrets:
    """Secretos leídos del entorno. Nunca imprimir ni loguear."""

    telegram_bot_token: str
    telegram_user_id: int
    anthropic_api_key: str
    data_key: str
    backup_key: str
    export_key: str
    data_key_previous: tuple[str, ...] = ()
    google_client_id: str = ""
    google_client_secret: str = ""
    brave_api_key: str = ""
    google_stt_api_key: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:
        return "Secrets(<redactado>)"

    __str__ = __repr__

    def all_values(self) -> list[str]:
        """Valores secretos para el filtro de redacción de logs."""
        values = [
            self.telegram_bot_token,
            self.anthropic_api_key,
            self.data_key,
            self.backup_key,
            self.export_key,
            self.google_client_secret,
            self.brave_api_key,
            self.google_stt_api_key,
            *self.data_key_previous,
        ]
        return [v for v in values if v and len(v) >= 8]

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None, *, require_all: bool = True) -> Secrets:
        source = dict(os.environ if env is None else env)
        if require_all:
            missing = [k for k in _REQUIRED_SECRETS if not source.get(k, "").strip()]
            if missing:
                raise ConfigError(
                    "Faltan secretos en el entorno (.env del servidor): " + ", ".join(missing)
                )
        raw_uid = source.get("TELEGRAM_USER_ID", "0").strip() or "0"
        try:
            user_id = int(raw_uid)
        except ValueError as exc:
            raise ConfigError("TELEGRAM_USER_ID debe ser un entero") from exc
        previous = tuple(
            k.strip() for k in source.get("MARC_DATA_KEY_PREVIOUS", "").split(",") if k.strip()
        )
        return cls(
            telegram_bot_token=source.get("TELEGRAM_BOT_TOKEN", "").strip(),
            telegram_user_id=user_id,
            anthropic_api_key=source.get("ANTHROPIC_API_KEY", "").strip(),
            data_key=source.get("MARC_DATA_KEY", "").strip(),
            backup_key=source.get("MARC_BACKUP_KEY", "").strip(),
            export_key=source.get("MARC_EXPORT_KEY", "").strip(),
            data_key_previous=previous,
            google_client_id=source.get("GOOGLE_CLIENT_ID", "").strip(),
            google_client_secret=source.get("GOOGLE_CLIENT_SECRET", "").strip(),
            brave_api_key=source.get("BRAVE_API_KEY", "").strip(),
            google_stt_api_key=source.get("GOOGLE_STT_API_KEY", "").strip(),
        )
