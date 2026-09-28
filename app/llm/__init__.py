"""LLM client factory."""

from __future__ import annotations

from app.config import Settings
from app.llm.pricing import override_price
from app.llm.types import LLMClient, LLMError


def build_llm(settings: Settings, *, model: str | None = None) -> LLMClient:
    """Build the configured LLM client. `model` overrides the default (eval simulator/judge)."""
    provider = settings.resolved_llm_provider
    if provider == "ollama":
        from app.llm.ollama_client import OllamaClient

        return OllamaClient(
            model or settings.ollama_model,
            base_url=settings.ollama_base_url,
            timeout_s=settings.llm_timeout_s,
            num_ctx=settings.ollama_num_ctx,
        )
    price = override_price(
        settings.llm_price_input_usd_per_mtok, settings.llm_price_output_usd_per_mtok
    )
    if provider == "anthropic":
        from app.llm.anthropic_client import AnthropicClient

        return AnthropicClient(
            settings.llm_api_key,
            model or settings.llm_model,
            effort=settings.llm_effort,
            timeout_s=settings.llm_timeout_s,
            price=price,
        )
    if not settings.hf_token:
        raise LLMError("HF_TOKEN is not set", retryable=False, code="not_configured")
    from app.llm.hf_client import HuggingFaceClient

    return HuggingFaceClient(
        settings.hf_token,
        model or settings.hf_model,
        timeout_s=settings.llm_timeout_s,
        price_override=price,
    )


def eval_model_name(settings: Settings) -> str | None:
    """Model used by the eval customer simulator and LLM judge (None = same as the agent)."""
    provider = settings.resolved_llm_provider
    if provider == "ollama":
        return settings.ollama_eval_model or None
    if provider == "huggingface":
        return settings.hf_eval_model or None
    return None


__all__ = ["LLMClient", "LLMError", "build_llm", "eval_model_name"]
