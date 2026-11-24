"""
Application settings.

All configuration comes from environment variables (12-factor). In AWS, these are
injected from SSM Parameter Store / Secrets Manager by the ECS task definition —
secrets are never baked into the image.
"""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- App -----------------------------------------------------------------
    app_name: str = "AR Wall Painting SaaS"
    environment: Literal["local", "test", "staging", "production"] = "local"
    api_prefix: str = "/v1"
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:3000"])
    log_level: str = "INFO"

    # --- Database --------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/arwall"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_statement_timeout_ms: int = 15_000

    # --- JWT -----------------------------------------------------------------
    # Production uses asymmetric keys (ES256) so AI workers and other services
    # can verify tokens with the public key without being able to mint them.
    jwt_algorithm: Literal["HS256", "RS256", "ES256"] = "HS256"
    jwt_secret: SecretStr | None = None            # HS256 only (local/test)
    jwt_private_key: SecretStr | None = None       # PEM, RS256/ES256
    jwt_public_key: str | None = None              # PEM, RS256/ES256
    jwt_issuer: str = "https://api.arwallpaint.app"
    jwt_audience: str = "arwall-mobile"
    access_token_ttl_seconds: int = 15 * 60
    refresh_token_ttl_days: int = 30

    # --- Auth hardening ------------------------------------------------------
    max_failed_logins: int = 5
    lockout_minutes: int = 15

    # --- AWS -----------------------------------------------------------------
    aws_region: str = "ap-south-1"
    s3_bucket: str = "arwall-user-media"
    s3_presign_ttl_seconds: int = 900
    s3_max_upload_mb: int = 25
    sqs_ai_queue_url: str | None = None            # None → jobs are logged, not sent (local dev)

    # --- Stripe --------------------------------------------------------------
    stripe_secret_key: SecretStr | None = None
    stripe_webhook_secret: SecretStr | None = None
    checkout_success_url: str = "arwall://billing/success?session_id={CHECKOUT_SESSION_ID}"
    checkout_cancel_url: str = "arwall://billing/cancel"
    billing_portal_return_url: str = "arwall://settings/billing"

    # --- Internal service auth (AI workers → API callbacks) -------------------
    internal_service_token: SecretStr | None = None

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @model_validator(mode="after")
    def _validate_security(self) -> "Settings":
        if self.jwt_algorithm == "HS256":
            if not self.jwt_secret or len(self.jwt_secret.get_secret_value()) < 32:
                raise ValueError("HS256 requires JWT_SECRET of at least 32 characters")
            if self.environment == "production":
                raise ValueError("Use RS256/ES256 in production")
        elif not (self.jwt_private_key and self.jwt_public_key):
            raise ValueError(f"{self.jwt_algorithm} requires JWT_PRIVATE_KEY and JWT_PUBLIC_KEY")
        if self.environment == "production":
            missing = [n for n in ("stripe_secret_key", "stripe_webhook_secret", "internal_service_token")
                       if getattr(self, n) is None]
            if missing:
                raise ValueError(f"Missing production secrets: {', '.join(missing)}")
        return self

    @property
    def jwt_signing_key(self) -> str:
        if self.jwt_algorithm == "HS256":
            return self.jwt_secret.get_secret_value()  # type: ignore[union-attr]
        return self.jwt_private_key.get_secret_value()  # type: ignore[union-attr]

    @property
    def jwt_verify_key(self) -> str:
        if self.jwt_algorithm == "HS256":
            return self.jwt_secret.get_secret_value()  # type: ignore[union-attr]
        return self.jwt_public_key  # type: ignore[return-value]


@lru_cache
def get_settings() -> Settings:
    return Settings()
