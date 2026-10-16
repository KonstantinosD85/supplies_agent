"""Plays one eval case against the real agent service, with fake supplies tools.

Everything but the tools is real: the model, the system prompt, /invocations, the approval
gate, sessions and the fact store (a fresh SQLite file per case). A case is a short script of
steps, sent the way the web UI sends them:

    {"say": "Order 2 boxes of gloves"}                     a user message
    {"decide": "approve"}                                  approve (or decline) what is waiting
    {"say": "...", "user": "bob", "session": "s2"}         as another user or in another session

Steps and setup are plain JSON so they are saved with the results.
"""

import asyncio
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from strands.models import Model

from evals.fake_supplies import FakeSupplies
from supplies_agent.agent.app import create_app
from supplies_agent.agent.model import build_model
from supplies_agent.agent.schemas import SESSION_HEADER, USER_HEADER
from supplies_agent.agent.settings import AgentSettings
from supplies_agent.memory import facts

DEFAULT_USER = "alice"
DEFAULT_SESSION = "s1"
# The status of a "decide" step that was never sent, because no order was waiting.
NOTHING_TO_DECIDE = "not sent: no order was waiting for approval"


def _turn(step: dict[str, Any], session: str) -> dict[str, Any]:
    """An empty turn record; play fills in what happened."""
    return {
        "step": step,
        "session": session,
        "reply": "",
        "tools": [],
        "tool_calls": [],
        "pending": [],
    }


def _in_thread(coroutine):
    """Run a coroutine to completion even if the caller is already inside an event loop."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coroutine).result()


async def _profile(db_path: str, user: str) -> dict[str, str]:
    entries = await facts.store(db_path, user).search("")
    return dict(entry.content.split(": ", 1) for entry in entries)


async def _save_facts(db_path: str, saved: dict[str, dict[str, str]]) -> None:
    for user, values in saved.items():
        for key, value in values.items():
            await facts.store(db_path, user).add(value, {"key": key})


def play(
    steps: list[dict[str, Any]],
    setup: dict[str, Any] | None = None,
    settings: AgentSettings | None = None,
    model_factory: Callable[[AgentSettings], Model] = build_model,
) -> dict[str, Any]:
    """Run one case on a fresh database and fresh fake supplies. Returns what happened, as JSON.

    setup keys (all optional):
        saved_facts:  {user: {key: value}} saved before the first step
        stock:        {sku: quantity} overrides on the seed stock
        unavailable:  tool names that fail as if the supplies system were down ("*" for all)
    """
    setup = setup or {}
    supplies = FakeSupplies(
        stock=setup.get("stock", {}), unavailable=set(setup.get("unavailable", []))
    )

    @contextmanager
    def fake_tools(settings, ctx):
        yield supplies.tools()

    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "agent.db")
        settings = (settings or AgentSettings()).model_copy(update={"db_path": db_path})
        app = create_app(settings, model_factory=model_factory, tool_source=fake_tools)
        saved = setup.get("saved_facts", {})
        _in_thread(_save_facts(db_path, saved))

        client = TestClient(app)
        turns: list[dict[str, Any]] = []
        for step in steps:
            user = step.get("user", DEFAULT_USER)
            session = step.get("session", DEFAULT_SESSION)
            resumed: list[dict] = []
            if "say" in step:
                body: dict[str, Any] = {"message": step["say"]}
            else:
                resumed = next(
                    (t["pending"] for t in reversed(turns) if t["session"] == session), []
                )
                if not resumed:
                    # The agent never proposed an order, so the user has no button to click.
                    turns.append({**_turn(step, session), "status": NOTHING_TO_DECIDE})
                    continue
                body = {
                    "approvals": [
                        {"interrupt_id": p["interrupt_id"], "decision": step["decide"]}
                        for p in resumed
                    ]
                }
            response = client.post(
                "/invocations", json=body, headers={USER_HEADER: user, SESSION_HEADER: session}
            )
            data = response.json()
            ok = response.status_code == 200
            calls = data.get("tool_calls", []) if ok else []
            turns.append(
                {
                    **_turn(step, session),
                    "status": response.status_code,
                    "reply": data.get("reply", "") if ok else str(data.get("detail", data)),
                    # Tools the model asked for in this turn: an approved order counts in the
                    # turn that proposed it, not again when it resumes.
                    "tools": [c["name"] for c in calls[len(resumed) :]],
                    "tool_calls": calls,
                    "pending": data.get("pending_approvals", []) if ok else [],
                }
            )

        users = {step.get("user", DEFAULT_USER) for step in steps} | set(saved)
        profiles = {user: _in_thread(_profile(db_path, user)) for user in sorted(users)}

    return {
        "turns": turns,
        "trajectory": [name for turn in turns for name in turn["tools"]],
        "supply_calls": [{"tool": name, "input": args} for name, args in supplies.calls],
        "orders": supplies.orders,
        "saved_facts": profiles,
    }


def transcript(run: dict[str, Any]) -> str:
    """The conversation as text, for the report and the LLM judge."""
    lines = []
    for turn in run["turns"]:
        step = turn["step"]
        said = step["say"] if "say" in step else f"[clicks {step['decide']}]"
        lines.append(f"User ({step.get('user', DEFAULT_USER)}): {said}")
        if turn["tools"]:
            lines.append(f"  tools: {', '.join(turn['tools'])}")
        for p in turn["pending"]:
            lines.append(f"  [purchase order waiting for approval: deliver_to={p['deliver_to']}]")
        if turn["status"] == NOTHING_TO_DECIDE:
            lines.append(f"  [{NOTHING_TO_DECIDE}]")
        else:
            lines.append(f"Agent: {turn['reply']}")
    return "\n".join(lines)
