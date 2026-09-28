"""Service container: everything a request handler needs, built once at startup."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from app.agent.flags import AgentFlags
from app.agent.loop import AgentConfig, SupportAgent
from app.config import Settings
from app.db.seed import seed
from app.db.session import Database
from app.guardrails.budget import BudgetGuard
from app.guardrails.ratelimit import SlidingWindowLimiter
from app.llm import LLMClient, LLMError, build_llm
from app.logging_setup import get_logger
from app.money import inr_to_paise
from app.rag.base import Retriever

log = get_logger(__name__)


@dataclass
class Services:
    settings: Settings
    db: Database
    llm: LLMClient | None = None
    retriever: Retriever | None = None
    agent: SupportAgent | None = None
    budget: BudgetGuard | None = None
    ip_limiter: SlidingWindowLimiter | None = None
    session_limiter: SlidingWindowLimiter | None = None
    kb_ready: bool = False
    startup_errors: list[str] = field(default_factory=list)

    @property
    def llm_provider_name(self) -> str:
        return f"{self.llm.provider}:{self.llm.model}" if self.llm else "none"

    def startup(self) -> None:
        s = self.settings
        if s.hf_token:
            # Authenticates Hugging Face Hub downloads (embedding model) as well as the router.
            os.environ.setdefault("HF_TOKEN", s.hf_token)
        self.db.create_all()
        if s.seed_on_startup and seed(self.db):
            log.info("database seeded with demo data")
        if self.retriever is None:
            from app.rag.retriever import build_retriever

            persist = None if s.app_env == "test" else s.chroma_dir
            self.retriever = build_retriever(s, persist_dir=persist)
        self.kb_ready = True
        if self.llm is None:
            try:
                self.llm = build_llm(s)
            except LLMError as exc:
                self.startup_errors.append(str(exc))
                log.warning("LLM not configured; chat will answer with a fallback message")
        self.budget = self.budget or BudgetGuard(self.db, inr_to_paise(s.daily_budget_inr))
        self.ip_limiter = self.ip_limiter or SlidingWindowLimiter(s.rate_limit_ip_per_min)
        self.session_limiter = self.session_limiter or SlidingWindowLimiter(
            s.rate_limit_session_per_min
        )
        self.agent = SupportAgent(
            self.db,
            self.llm,
            retriever=self.retriever,
            config=AgentConfig(
                max_iterations=s.agent_max_iterations,
                tool_timeout_s=s.tool_timeout_s,
                llm_max_retries=s.llm_max_retries,
                max_output_tokens=s.llm_max_output_tokens,
                usd_inr_rate=s.usd_inr_rate,
                rag_top_k=s.rag_top_k,
            ),
            flags=AgentFlags.production(),  # production never runs an ablation config
            budget=self.budget,
        )

    def shutdown(self) -> None:
        if self.agent is not None:
            self.agent.close()
        self.db.dispose()


def build_services(settings: Settings) -> Services:
    return Services(settings=settings, db=Database(settings.database_url))
