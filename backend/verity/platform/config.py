"""Typed application configuration.

All configuration comes from environment variables (PRD §28.1: no secrets in
images, managed secret store in production). Settings are validated at import
time so a misconfigured process fails fast rather than at first request.
"""

from __future__ import annotations

import base64
from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    LOCAL = "local"
    PREVIEW = "preview"
    STAGING = "staging"
    PRODUCTION = "production"

    @property
    def is_production_like(self) -> bool:
        return self in (Environment.STAGING, Environment.PRODUCTION)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VERITY_",
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # ── Runtime ──────────────────────────────────────────────────────
    env: Environment = Environment.LOCAL
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    service_name: str = "verity-api"
    region: Literal["us", "eu"] = "us"

    # ── API ──────────────────────────────────────────────────────────
    api_host: str = "127.0.0.1"
    api_port: int = 8000
    public_api_url: str = "http://127.0.0.1:8000"
    public_web_url: str = "http://127.0.0.1:3000"
    cors_origins: str = "http://127.0.0.1:3000,http://localhost:3000"

    # ── Database ─────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://verity:verity@127.0.0.1:5432/verity"
    database_url_sync: str = "postgresql+psycopg://verity:verity@127.0.0.1:5432/verity"
    db_pool_size: int = 10
    db_max_overflow: int = 10
    db_echo: bool = False

    # ── Redis ────────────────────────────────────────────────────────
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_session_url: str = "redis://127.0.0.1:6379/1"
    redis_queue_url: str = "redis://127.0.0.1:6379/2"

    # ── Auth (PRD §10.2, §28.2) ──────────────────────────────────────
    jwt_private_key_pem: SecretStr = SecretStr("")
    jwt_public_key_pem: str = ""
    jwt_kid: str = "dev-1"
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 2_592_000
    session_cookie_name: str = "__Host-verity_session"
    secure_cookies: bool = False
    argon2_memory_kib: int = 65_536
    argon2_time_cost: int = 3
    argon2_parallelism: int = 1

    # ── OAuth ────────────────────────────────────────────────────────
    google_client_id: str = ""
    google_client_secret: SecretStr = SecretStr("")
    apple_client_id: str = ""

    # ── Encryption (PRD §28.1) ───────────────────────────────────────
    data_encryption_key: SecretStr = SecretStr("")

    # ── Storage (PRD §45) ────────────────────────────────────────────
    storage_backend: Literal["local", "s3"] = "local"
    storage_local_path: str = ".data/objects"
    s3_bucket: str = ""
    s3_region: str = ""
    s3_endpoint_url: str = ""

    # ── AI providers (PRD §20.2) ─────────────────────────────────────
    llm_primary_provider: Literal["stub", "anthropic", "groq"] = "stub"
    anthropic_api_key: SecretStr = SecretStr("")
    groq_api_key: SecretStr = SecretStr("")
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_fast_model: str = "llama-3.1-8b-instant"
    groq_realtime_model: str = "openai/gpt-oss-20b"
    groq_primary_model: str = "openai/gpt-oss-120b"
    openai_api_key: SecretStr = SecretStr("")
    embedding_provider: str = "local"
    embedding_dimensions: int = 1536

    # ── STT (PRD §23) ────────────────────────────────────────────────
    stt_primary_provider: str = "null"
    #: How long an interviewer may pause mid-sentence before the next words are
    #: treated as a new question instead of the rest of this one. Speech pacing
    #: is a property of the person and the room, so it is tunable without a
    #: deploy. The STT flush (700 ms) is already spent before this window opens,
    #: so the tolerated pause is roughly this value plus that.
    question_accumulation_window_ms: int = 2_000

    # ── Cost controls (PRD §33) ──────────────────────────────────────
    ai_daily_budget_usd: float = 250.0
    ai_budget_soft_threshold: float = 0.8
    session_max_generations: int = 60
    session_max_generations_per_minute: int = 8
    session_max_duration_minutes: int = 120

    # ── Billing (PRD §19) ────────────────────────────────────────────
    payment_provider: Literal["stub", "stripe"] = "stub"
    stripe_secret_key: SecretStr = SecretStr("")
    stripe_webhook_secret: SecretStr = SecretStr("")

    # ── Email ────────────────────────────────────────────────────────
    email_provider: Literal["console", "smtp", "resend"] = "console"
    email_from: str = "no-reply@verity.local"

    # ── Observability (PRD §30) ──────────────────────────────────────
    otel_enabled: bool = False
    otel_exporter_otlp_endpoint: str = ""
    trace_sample_ratio: float = 1.0

    # ── Rate limiting ────────────────────────────────────────────────
    #: Grace period before an account deletion executes (PRD FR-AUTH-009).
    #: Configurable so a test or a support-driven immediate erasure does not
    #: need a second code path.
    deletion_grace_days: int = 7

    rate_limit_enabled: bool = True

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {sorted(allowed)}")
        return upper

    @field_validator("jwt_private_key_pem", mode="before")
    @classmethod
    def _unescape_private_pem(cls, v: object) -> object:
        """PEMs are stored single-line in .env with escaped newlines."""
        if isinstance(v, str):
            return v.replace("\\n", "\n")
        return v

    @field_validator("jwt_public_key_pem", mode="before")
    @classmethod
    def _unescape_public_pem(cls, v: object) -> object:
        if isinstance(v, str):
            return v.replace("\\n", "\n")
        return v

    @field_validator("data_encryption_key")
    @classmethod
    def _validate_dek(cls, v: SecretStr) -> SecretStr:
        raw = v.get_secret_value()
        if not raw:
            return v
        try:
            decoded = base64.b64decode(raw, validate=True)
        except Exception as exc:  # pragma: no cover - defensive
            raise ValueError("data_encryption_key must be base64") from exc
        if len(decoded) != 32:
            raise ValueError("data_encryption_key must decode to exactly 32 bytes")
        return v

    @model_validator(mode="after")
    def _production_requirements(self) -> Settings:
        """Fail fast when a production-like env is missing mandatory secrets."""
        if not self.env.is_production_like:
            return self
        missing: list[str] = []
        if not self.jwt_private_key_pem.get_secret_value():
            missing.append("JWT_PRIVATE_KEY_PEM")
        if not self.jwt_public_key_pem:
            missing.append("JWT_PUBLIC_KEY_PEM")
        if not self.data_encryption_key.get_secret_value():
            missing.append("DATA_ENCRYPTION_KEY")
        if not self.secure_cookies:
            missing.append("SECURE_COOKIES must be true")
        if missing:
            raise ValueError("Missing required production configuration: " + ", ".join(missing))
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def debug(self) -> bool:
        return self.env == Environment.LOCAL


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings: Settings = get_settings()
