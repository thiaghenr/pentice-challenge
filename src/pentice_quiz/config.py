from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

LLMProvider = Literal["openai", "openrouter", "ollama"]


class Settings(BaseSettings):
    # env_ignore_empty: `OPENAI_API_KEY=` (as copied from .env.example) means "not set".
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", env_ignore_empty=True, extra="ignore"
    )

    # LLM
    llm_provider: LLMProvider = "openai"
    llm_model: str = "gpt-4.1-mini"
    llm_fallback_provider: LLMProvider | None = None
    llm_fallback_model: str | None = None
    llm_timeout_seconds: float = Field(default=20.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0, le=2)
    openai_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None
    ollama_base_url: str = "http://localhost:11434/v1"

    # House rules source
    house_rules_url: str | None = None
    source_timeout_seconds: float = Field(default=10.0, gt=0)
    source_max_bytes: int = Field(default=1_000_000, gt=0)

    # Persistence and operations
    database_url: str = "postgresql+psycopg://pentice:pentice@localhost:5432/pentice"
    ops_api_key: SecretStr | None = None
    pass_threshold_pct: float = Field(default=75.0, ge=0, le=100)
    missed_min_attempts: int = Field(default=3, ge=1)
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
