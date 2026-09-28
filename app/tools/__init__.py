"""Tool registry. Tool order is fixed so the LLM request prefix stays cacheable."""

from __future__ import annotations

from app.agent.flags import AgentFlags
from app.tools.account import NoArgs, VerifyArgs, get_order, list_orders, verify_customer
from app.tools.actions import (
    AddressArgs,
    CancelArgs,
    RefundArgs,
    cancel_order,
    issue_refund,
    update_address,
)
from app.tools.base import OrderRef, ToolContext, ToolResult, ToolSpec, run_tool
from app.tools.support import EscalateArgs, SearchArgs, escalate_to_human, search_help_center

REGISTRY: dict[str, ToolSpec] = {
    spec.name: spec
    for spec in [
        ToolSpec(
            "verify_customer",
            "Verify the customer's identity. Needs the account email, one of their order "
            "numbers and that order's 6-digit delivery PIN code. Required before any account "
            "data is shared or any action is taken.",
            VerifyArgs,
            verify_customer,
        ),
        ToolSpec(
            "list_orders",
            "List the verified customer's orders (newest first).",
            NoArgs,
            list_orders,
        ),
        ToolSpec(
            "get_order",
            "Get full details of one of the verified customer's orders: status, items, "
            "payment method, timeline, courier/tracking, refund status.",
            OrderRef,
            get_order,
        ),
        ToolSpec(
            "search_help_center",
            "Search Acme Outdoors help-center articles (returns, refunds, shipping, COD, "
            "GST invoices, warranty, payments...). Use for any policy question.",
            SearchArgs,
            search_help_center,
        ),
        ToolSpec(
            "cancel_order",
            "Cancel an order. Call it as soon as a verified customer asks to cancel; the system "
            "checks eligibility (only unshipped orders) and the reason is optional.",
            CancelArgs,
            cancel_order,
            is_action=True,
        ),
        ToolSpec(
            "issue_refund",
            "Refund an order to the original payment method. Call it as soon as a verified "
            "customer asks; for a full refund use total_paise from get_order. amount_paise is an "
            "integer in paise. The system enforces the business rules; the result says whether "
            "it was issued, needs approval, or is not allowed and why. Reason is optional.",
            RefundArgs,
            issue_refund,
            is_action=True,
        ),
        ToolSpec(
            "update_address",
            "Change the delivery address of an order. Call it as soon as a verified customer "
            "gives the new address; the system checks eligibility (only unshipped orders).",
            AddressArgs,
            update_address,
            is_action=True,
        ),
        ToolSpec(
            "escalate_to_human",
            "Hand the conversation to a human support specialist with a reason and urgency. "
            "Use when the customer asks for a human, is very upset, mentions legal action, "
            "or when you are unsure.",
            EscalateArgs,
            escalate_to_human,
        ),
    ]
}


def available_tools(flags: AgentFlags, *, allow_actions: bool = True) -> list[ToolSpec]:
    tools = []
    for spec in REGISTRY.values():
        if spec.name == "search_help_center" and not flags.rag:
            continue
        if spec.is_action and not allow_actions:
            continue
        tools.append(spec)
    return tools


__all__ = [
    "REGISTRY",
    "ToolContext",
    "ToolResult",
    "ToolSpec",
    "available_tools",
    "run_tool",
]
