"""Tool plumbing: typed arguments, structured results, the execution context, the registry."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ValidationError, field_validator

from app.agent.flags import AgentFlags
from app.agent.state import ConversationState
from app.agent.tracing import Tracer
from app.db.session import Database
from app.policy.engine import DEFAULT_POLICY, PolicyConfig
from app.rag.base import Retriever

ORDER_NUMBER_RE = re.compile(r"^AO-\d{5}$")


class ToolResult(BaseModel):
    """Uniform result shape: {ok, data | error_code, message}."""

    ok: bool
    message: str
    data: dict[str, Any] | None = None
    error_code: str | None = None

    @classmethod
    def success(cls, message: str, data: dict[str, Any] | None = None) -> ToolResult:
        return cls(ok=True, message=message, data=data or {})

    @classmethod
    def failure(
        cls, error_code: str, message: str, data: dict[str, Any] | None = None
    ) -> ToolResult:
        return cls(ok=False, error_code=error_code, message=message, data=data)


def normalise_order_number(value: str) -> str:
    """Accept 'ao 10001', 'AO10001', '#10001' or '10001' and return 'AO-10001'."""
    cleaned = re.sub(r"[\s#_]", "", str(value)).upper()
    if cleaned.isdigit():
        cleaned = f"AO-{cleaned}"
    elif cleaned.startswith("AO") and not cleaned.startswith("AO-"):
        cleaned = "AO-" + cleaned[2:]
    if not ORDER_NUMBER_RE.match(cleaned):
        raise ValueError("Order numbers look like AO-10001")
    return cleaned


class OrderRef(BaseModel):
    order_number: str

    @field_validator("order_number")
    @classmethod
    def _order_number(cls, value: str) -> str:
        return normalise_order_number(value)


@dataclass
class ToolContext:
    db: Database
    state: ConversationState
    conversation_id: str
    now: datetime
    tracer: Tracer
    flags: AgentFlags = field(default_factory=AgentFlags.production)
    policy: PolicyConfig = DEFAULT_POLICY
    retriever: Retriever | None = None
    transcript: list[dict[str, str]] = field(default_factory=list)
    rag_top_k: int = 4


Handler = Callable[[ToolContext, Any], ToolResult]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    handler: Handler
    is_action: bool = False

    def input_schema(self) -> dict[str, Any]:
        schema = self.args_model.model_json_schema()
        return _strip_titles(schema)


def _strip_titles(node: Any) -> Any:
    if isinstance(node, dict):
        return {k: _strip_titles(v) for k, v in node.items() if k != "title"}
    if isinstance(node, list):
        return [_strip_titles(v) for v in node]
    return node


def format_validation_error(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "arguments"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def run_tool(spec: ToolSpec, ctx: ToolContext, raw_args: dict[str, Any]) -> ToolResult:
    """Validate arguments and run the handler. Never raises."""
    try:
        args = spec.args_model.model_validate(raw_args or {})
    except ValidationError as exc:
        return ToolResult.failure("INVALID_ARGUMENTS", format_validation_error(exc))
    try:
        return spec.handler(ctx, args)
    except Exception as exc:  # a tool bug must become a structured error, not a crash
        ctx.tracer.record("error", f"tool:{spec.name}", {"error": f"{type(exc).__name__}: {exc}"})
        return ToolResult.failure(
            "INTERNAL_ERROR",
            "Something went wrong on our side while doing that. Nothing was changed.",
        )
