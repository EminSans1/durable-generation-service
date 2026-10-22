import logging
from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://generation:generation@localhost:5432/generation"
    alpha_url: str = "http://alpha:8001"
    beta_url: str = "http://beta:8001"
    callback_base_url: str = "http://api:8000"
    alpha_secret: str = "local-alpha-secret"
    beta_secret: str = "local-beta-secret"
    payment_secret: str = "local-payment-secret"
    provider_api_key: str = "local-provider-api-key"
    provider_timeout: float = Field(default=60, gt=0)
    http_timeout: float = Field(default=5, gt=0)
    dispatch_lease: float = Field(default=15, gt=0)
    worker_poll: float = Field(default=0.25, gt=0)
    provider_name: str = "alpha"
    provider_delay_min: float = Field(default=2, ge=0)
    provider_delay_max: float = Field(default=20, ge=0)
    log_level: str = "INFO"

    @model_validator(mode="after")
    def validate_intervals(self) -> "Settings":
        if self.dispatch_lease <= self.http_timeout:
            raise ValueError("DISPATCH_LEASE must exceed HTTP_TIMEOUT")
        if self.provider_delay_max < self.provider_delay_min:
            raise ValueError("PROVIDER_DELAY_MAX must be >= PROVIDER_DELAY_MIN")
        if self.provider_name not in {"alpha", "beta"}:
            raise ValueError("PROVIDER_NAME must be alpha or beta")
        return self

    def secret(self, provider: str) -> str:
        return self.alpha_secret if provider == "alpha" else self.beta_secret

    def provider_url(self, provider: str) -> str:
        return self.alpha_url if provider == "alpha" else self.beta_url


@lru_cache
def get_settings() -> Settings:
    return Settings()


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s pid=%(process)d %(name)s %(message)s",
    )
