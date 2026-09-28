"""Feature flags for evaluation ablations only.

Production always runs `AgentFlags.production()`; nothing reads these from the
environment, so a deployment cannot switch a safety layer off by configuration.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AgentFlags:
    policy: bool = True  # code-level policy enforcement inside action tools
    guardrails: bool = True  # injection gating, output checks
    rag: bool = True  # search_help_center tool
    router: bool = True  # intent router + automatic escalation triggers

    @classmethod
    def production(cls) -> AgentFlags:
        return cls()


EVAL_CONFIGS: dict[str, AgentFlags] = {
    "baseline": AgentFlags(policy=False, guardrails=False, rag=False, router=False),
    "policy": AgentFlags(policy=True, guardrails=False, rag=False, router=False),
    "guardrails": AgentFlags(policy=True, guardrails=True, rag=False, router=False),
    "rag": AgentFlags(policy=True, guardrails=True, rag=True, router=False),
    "full": AgentFlags(policy=True, guardrails=True, rag=True, router=True),
}

EVAL_CONFIG_LABELS: dict[str, str] = {
    "baseline": "Baseline (prompt only)",
    "policy": "+ policy engine",
    "guardrails": "+ guardrails",
    "rag": "+ RAG",
    "full": "+ router (full)",
}
