"""Approval gate: create_purchase_order never runs until the user approves that exact order.

The hook interrupts the agent before the tool runs. The turn ends with the proposed order;
the user's next request carries a structured decision ("approve" or "decline") that resumes
the agent. The decision comes from the UI or API, never from the model's reading of the chat.
Every order is approved on its own: approvals are never remembered.
"""

import logging
from typing import Any

from strands import Agent
from strands.agent import AgentResult
from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry
from strands.session import SessionManager

from supplies_agent.agent.schemas import ChatRequest, PendingApproval

logger = logging.getLogger(__name__)

GATED_TOOL = "create_purchase_order"
INTERRUPT_NAME = "po-approval"

DECLINED_MESSAGE = (
    "The user declined this purchase order, so it was not placed. Tell the user it was not "
    "placed, and do not place it again unless they ask."
)

MISSING_DELIVERY_MESSAGE = (
    "deliver_to is required. Ask the user where to deliver the order (or use their saved "
    "delivery_location), then call create_purchase_order again with it."
)


class ApprovalHook(HookProvider):
    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self.require_approval)

    def require_approval(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use["name"] != GATED_TOOL:
            return

        order = event.tool_use["input"]
        if not str(order.get("deliver_to") or "").strip():
            # Never show the user an order that can't be placed: send the model back to ask.
            event.cancel_tool = MISSING_DELIVERY_MESSAGE
            return
        # First pass: raises an interrupt and ends the turn. On resume: returns the decision.
        decision = event.interrupt(
            INTERRUPT_NAME,
            reason={"lines": order.get("lines", []), "deliver_to": order["deliver_to"]},
        )
        logger.info(
            "tool_use_id=<%s>, decision=<%s> | purchase order approval decided",
            event.tool_use["toolUseId"],
            decision,
        )
        if decision != "approve":
            event.cancel_tool = DECLINED_MESSAGE


# --- Pending approvals between turns ---------------------------------------------------------
# Strands persists the interrupt itself in the session. We also keep a copy of what is waiting
# in agent.state (public and session-persisted), so a request can be checked against it before
# the agent runs: a message while an order waits, or a decision when nothing waits, is refused.

PENDING_KEY = "pending_approvals"


class ApprovalConflict(Exception):
    """The request doesn't fit what is (or isn't) waiting for approval."""

    def __init__(self, message: str, pending: list[PendingApproval]) -> None:
        super().__init__(message)
        self.message = message
        self.pending = pending


def pending_approvals(agent: Agent) -> list[PendingApproval]:
    return [PendingApproval(**p) for p in agent.state.get(PENDING_KEY) or []]


def agent_input(request: ChatRequest, pending: list[PendingApproval]) -> str | list[dict]:
    """What to invoke the agent with: the user's message, or interrupt responses that resume it."""
    if request.message is not None:
        if pending:
            raise ApprovalConflict(
                "A purchase order is waiting for your decision. Approve or decline it first.",
                pending,
            )
        return request.message

    assert request.approvals is not None
    if not pending:
        raise ApprovalConflict("No purchase order is waiting for approval.", [])
    if {a.interrupt_id for a in request.approvals} != {p.interrupt_id for p in pending}:
        raise ApprovalConflict(
            "Send exactly one decision for each purchase order that is waiting.", pending
        )
    return [
        {"interruptResponse": {"interruptId": a.interrupt_id, "response": a.decision}}
        for a in request.approvals
    ]


def record_pending(
    agent: Agent, result: AgentResult, session_manager: SessionManager
) -> list[PendingApproval]:
    """Store what the turn left waiting for approval (or clear it), and return it."""
    pending = [
        PendingApproval(interrupt_id=i.id, **i.reason)
        for i in result.interrupts or []
        if i.name == INTERRUPT_NAME
    ]
    agent.state.set(PENDING_KEY, [p.model_dump() for p in pending])
    session_manager.sync_agent(agent)  # the turn has ended, so persist the change ourselves
    for p in pending:
        logger.info(
            "interrupt_id=<%s> | purchase order proposed, waiting for approval", p.interrupt_id
        )
    return pending
