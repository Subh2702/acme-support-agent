"""Application settings, loaded from environment variables (and `.env` locally).

Every variable here is documented in `.env.example`. Secrets are only ever read
from the environment; nothing secret has a non-empty default.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["development", "production", "test"] = "development"
    log_level: str = "INFO"

    # --- Database -------------------------------------------------------
    database_url: str = f"sqlite:///{ROOT_DIR / 'data' / 'acme.db'}"
    seed_on_startup: bool = True

    # --- LLM --------------------------------------------------------------
    # `auto`: anthropic if LLM_API_KEY is set, else huggingface if HF_TOKEN is set, else a
    # local small model served by Ollama.
    llm_provider: Literal["auto", "anthropic", "huggingface", "ollama"] = "auto"
    llm_api_key: str = ""
    llm_model: str = "claude-opus-5"
    llm_effort: Literal["low", "medium", "high"] = "medium"
    hf_token: str = ""
    # `:cheapest` lets the HF router pick the lowest-priced provider for the model.
    hf_model: str = "Qwen/Qwen3-235B-A22B-Instruct-2507:cheapest"
    # Cheaper model for the eval customer simulator and LLM judge.
    hf_eval_model: str = "Qwen/Qwen3-235B-A22B-Instruct-2507:cheapest"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen3:4b-instruct"
    ollama_eval_model: str = ""  # simulator/judge model; defaults to OLLAMA_MODEL
    ollama_num_ctx: int = 8192
    llm_timeout_s: float = 180.0
    llm_max_retries: int = 3
    llm_max_output_tokens: int = 600
    # Optional price override (USD per million tokens) for models not in the price table.
    llm_price_input_usd_per_mtok: float | None = None
    llm_price_output_usd_per_mtok: float | None = None

    # --- Money / cost -------------------------------------------------------
    usd_inr_rate: float = 85.0
    daily_budget_inr: float = 300.0

    # --- Agent --------------------------------------------------------------
    agent_max_iterations: int = 6
    tool_timeout_s: float = 8.0

    # --- RAG ------------------------------------------------------------------
    kb_dir: Path = ROOT_DIR / "app" / "rag" / "kb"
    chroma_dir: Path = ROOT_DIR / "data" / "chroma"
    embedding_backend: Literal["fastembed", "hash"] = "fastembed"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    fastembed_cache_dir: Path = ROOT_DIR / "data" / "models"
    rag_top_k: int = 4

    # --- Admin / abuse controls ---------------------------------------------------
    admin_user: str = "admin"
    admin_password: str = ""
    rate_limit_ip_per_min: int = 20
    rate_limit_session_per_min: int = 10
    max_message_chars: int = 2000
    # Salt for hashing client IPs before storing them (raw IPs are never stored).
    ip_hash_salt: str = "acme-demo-salt"

    @field_validator("database_url")
    @classmethod
    def _normalise_postgres_url(cls, value: str) -> str:
        """Render/Heroku hand out `postgres://`; SQLAlchemy needs an explicit driver."""
        if value.startswith("postgres://"):
            value = "postgresql://" + value.removeprefix("postgres://")
        if value.startswith("postgresql://"):
            value = "postgresql+psycopg://" + value.removeprefix("postgresql://")
        return value

    @property
    def resolved_llm_provider(self) -> Literal["anthropic", "huggingface", "ollama"]:
        if self.llm_provider != "auto":
            return self.llm_provider
        if self.llm_api_key:
            return "anthropic"
        return "huggingface" if self.hf_token else "ollama"

    @property
    def active_model(self) -> str:
        return {
            "anthropic": self.llm_model,
            "huggingface": self.hf_model,
            "ollama": self.ollama_model,
        }[self.resolved_llm_provider]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
