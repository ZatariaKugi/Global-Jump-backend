"""Application settings.

Official FastAPI pattern: a ``pydantic-settings`` ``BaseSettings`` subclass read from
the environment / ``.env`` file, exposed through an ``@lru_cache`` factory so the file is
parsed once and the object can be overridden in tests via dependency injection.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Any

from pydantic import AnyHttpUrl, Field, PostgresDsn, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    local = "local"
    staging = "staging"
    production = "production"
    test = "test"  # CI / automated runs


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Runtime ----------------------------------------------------------------
    ENVIRONMENT: Environment = Environment.local
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = False

    # API --------------------------------------------------------------------
    PROJECT_NAME: str = "GlobleJump API"
    API_V1_PREFIX: str = "/api/v1"
    CORS_ORIGINS: list[AnyHttpUrl] | list[str] = Field(default_factory=list)

    # Database ---------------------------------------------------------------
    DATABASE_URL: PostgresDsn | None = None
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_DB: str = "app_db"

    # Auth — tokens issued by this service ----------------------------------
    JWT_SECRET: str = "change-me"
    JWT_ALGORITHM: str = "HS256"
    JWT_ISSUER: str = "globlejump"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30

    # Auth — token lifetimes ------------------------------------------------
    REFRESH_TOKEN_EXPIRE_DAYS: int = 30
    EMAIL_VERIFY_TOKEN_EXPIRE_HOURS: int = 24
    PASSWORD_RESET_TOKEN_EXPIRE_MINUTES: int = 15

    # Auth — external identity-service trust --------------------------------
    IDENTITY_JWT_SECRET: str | None = None
    IDENTITY_JWKS_URL: AnyHttpUrl | None = None
    IDENTITY_ISSUER: str = "identity-service"
    IDENTITY_AUDIENCE: str = "globlejump"

    # Email ---------------------------------------------------------------
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_USER: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_TLS: bool = True
    SMTP_SSL: bool = False
    EMAILS_FROM: str = "noreply@globlejump.io"
    EMAILS_FROM_NAME: str = "GlobleJump"
    FRONTEND_URL: str = "http://localhost:3000"

    # Encryption & file storage ---------------------------------------------
    ENCRYPTION_KEY: str = ""  # base64url-encoded 32-byte key (AES-256)
    UPLOAD_DIR: str = "uploads"  # root directory for credential file uploads (local fallback)
    UPLOAD_MAX_MB: int = 10

    # File storage — S3 (used when AWS_ACCESS_KEY_ID/S3_BUCKET_NAME are set; otherwise
    # falls back to local disk storage under UPLOAD_DIR)
    AWS_ACCESS_KEY_ID: str | None = None
    AWS_SECRET_ACCESS_KEY: str | None = None
    AWS_REGION: str = "us-east-1"
    S3_BUCKET_NAME: str | None = None

    # Stripe -----------------------------------------------------------------
    STRIPE_SECRET_KEY: str | None = None
    STRIPE_WEBHOOK_SECRET: str | None = None
    STRIPE_PUBLISHABLE_KEY: str | None = None
    PLATFORM_COMMISSION_RATE: float = 0.15  # 15% platform commission, configurable
    TAX_WITHHOLDING_RATE: float = 0.08  # 8% tax withheld from advisor payouts, configurable
    PAYOUT_PROCESSING_FEE_RATE: float = 0.02  # 2% fee on manual payout requests, configurable
    INVOICE_FROM_ADDRESS: str | None = None  # optional platform address on invoices

    # OpenAI (AI assessment insights) -----------------------------------------
    OPENAI_API_KEY: str | None = None
    OPENAI_MODEL: str = "gpt-5.4-mini"
    OPENAI_TIMEOUT_SECONDS: float = 20.0

    # Observability ----------------------------------------------------------
    SENTRY_DSN: str | None = None
    OTEL_EXPORTER_OTLP_ENDPOINT: str | None = None
    OTEL_SERVICE_NAME: str = "globlejump"

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_cors(cls, value: Any) -> Any:
        # Allow a comma-separated string in addition to a JSON list.
        if isinstance(value, str) and not value.startswith("["):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator(
        "DATABASE_URL",
        "IDENTITY_JWKS_URL",
        "SENTRY_DSN",
        "SMTP_HOST",
        "STRIPE_SECRET_KEY",
        "STRIPE_WEBHOOK_SECRET",
        "STRIPE_PUBLISHABLE_KEY",
        "OPENAI_API_KEY",
        mode="before",
    )
    @classmethod
    def _empty_str_to_none(cls, value: Any) -> Any:
        # Optional fields are often present-but-blank in .env files.
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sqlalchemy_dsn(self) -> str:
        """Async SQLAlchemy DSN. Prefers DATABASE_URL, else composes from parts."""
        if self.DATABASE_URL is not None:
            return str(self.DATABASE_URL)
        dsn = PostgresDsn.build(
            scheme="postgresql+asyncpg",
            username=self.POSTGRES_USER,
            password=self.POSTGRES_PASSWORD,
            host=self.POSTGRES_HOST,
            port=self.POSTGRES_PORT,
            path=self.POSTGRES_DB,
        )
        return str(dsn)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return Environment.production == self.ENVIRONMENT

    @property
    def external_auth_enabled(self) -> bool:
        return bool(self.IDENTITY_JWT_SECRET or self.IDENTITY_JWKS_URL)


@lru_cache
def get_settings() -> Settings:
    """Cached settings factory (parses .env once). Override in tests."""
    return Settings()
