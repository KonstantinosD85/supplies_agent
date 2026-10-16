"""Evaluators: exact checks on what happened, and an LLM judge for what can't be checked exactly.

Every evaluator runs on every case, so each one reads what to check from the case's metadata:
ScenarioChecks runs the case's "checks", RubricJudge judges the case's "rubric" (and reports
"not applicable" for cases without one).
"""

from collections.abc import Callable
from typing import Any

from strands.models import Model
from strands_evals.evaluators import Evaluator, OutputEvaluator
from strands_evals.types import EvaluationData, EvaluationOutput
from strands_evals.types.evaluation import NOT_APPLICABLE

from supplies_agent.agent.model import close_model

RUN_STATE = "run"

# --- Exact checks ---------------------------------------------------------------------------
# Each takes the run (see harness.play) plus the check's arguments, and returns None when it
# holds or a short reason when it doesn't. "turn" picks one turn by index (-1 is the last).


def _turns(run: dict, turn: int | None) -> list[dict]:
    return run["turns"] if turn is None else [run["turns"][turn]]


def _tools(run: dict, turn: int | None) -> list[str]:
    return run["trajectory"] if turn is None else run["turns"][turn]["tools"]


def first_tool(run: dict, tool: str) -> str | None:
    first = run["trajectory"][0] if run["trajectory"] else None
    return None if first == tool else f"first tool was {first}, expected {tool}"


def used(run: dict, tools: list[str], turn: int | None = None) -> str | None:
    missing = [t for t in tools if t not in _tools(run, turn)]
    return f"never called {missing}" if missing else None


def not_used(run: dict, tools: list[str], turn: int | None = None) -> str | None:
    called = [t for t in tools if t in _tools(run, turn)]
    return f"called {called}" if called else None


def no_tools(run: dict) -> str | None:
    return f"called {run['trajectory']}" if run["trajectory"] else None


def max_calls(run: dict, tool: str, count: int) -> str | None:
    n = run["trajectory"].count(tool)
    return f"called {tool} {n} times, at most {count} expected" if n > count else None


def orders(run: dict, count: int) -> str | None:
    n = len(run["orders"])
    return f"{n} purchase orders placed, expected {count}" if n != count else None


def order_deliver_to(run: dict, contains: str) -> str | None:
    wrong = [o["deliver_to"] for o in run["orders"] if contains not in o["deliver_to"]]
    return f"orders delivered to {wrong}, expected {contains!r}" if wrong else None


def pending(run: dict, count: int, turn: int = -1) -> str | None:
    n = len(run["turns"][turn]["pending"])
    return f"{n} orders waiting for approval, expected {count}" if n != count else None


def pending_deliver_to(run: dict, contains: str, turn: int = -1) -> str | None:
    waiting = run["turns"][turn]["pending"]
    if not waiting:
        return "no order waiting for approval"
    wrong = [p["deliver_to"] for p in waiting if contains not in p["deliver_to"]]
    return f"waiting order delivers to {wrong}, expected {contains!r}" if wrong else None


def pending_line(run: dict, sku: str, quantity: int, turn: int = -1) -> str | None:
    lines = [line for p in run["turns"][turn]["pending"] for line in p["lines"]]
    if {"sku": sku, "quantity": quantity} in lines:
        return None
    return f"waiting order lines {lines}, expected {quantity} x {sku}"


def fact(run: dict, user: str, key: str, contains: str) -> str | None:
    value = run["saved_facts"].get(user, {}).get(key)
    if value is not None and contains.lower() in value.lower():
        return None
    return f"{user}'s {key} is {value!r}, expected it to contain {contains!r}"


def no_fact(run: dict, user: str, key: str) -> str | None:
    value = run["saved_facts"].get(user, {}).get(key)
    return f"{user}'s {key} was saved as {value!r}" if value is not None else None


def reply_includes_any(run: dict, texts: list[str], turn: int = -1) -> str | None:
    reply = run["turns"][turn]["reply"].lower()
    return None if any(t.lower() in reply for t in texts) else f"reply mentions none of {texts}"


def reply_excludes(run: dict, text: str, turn: int | None = None) -> str | None:
    found = [t["reply"] for t in _turns(run, turn) if text.lower() in t["reply"].lower()]
    return f"a reply contains {text!r}" if found else None


CHECKS: dict[str, Callable[..., str | None]] = {
    f.__name__: f
    for f in [
        first_tool,
        used,
        not_used,
        no_tools,
        max_calls,
        orders,
        order_deliver_to,
        pending,
        pending_deliver_to,
        pending_line,
        fact,
        no_fact,
        reply_includes_any,
        reply_excludes,
    ]
}


def run_state(case: EvaluationData) -> dict[str, Any]:
    return next(s.state for s in case.actual_environment_state or [] if s.name == RUN_STATE)


class ScenarioChecks(Evaluator[list, str]):
    """The case's exact checks, one row each. Every request must also have succeeded."""

    def evaluate(self, evaluation_case: EvaluationData[list, str]) -> list[EvaluationOutput]:
        run = run_state(evaluation_case)
        checks = [{"check": "all_requests_succeeded"}, *evaluation_case.metadata["checks"]]
        results = []
        for spec in checks:
            args = {k: v for k, v in spec.items() if k != "check"}
            if spec["check"] == "all_requests_succeeded":
                failed = [t["status"] for t in run["turns"] if t["status"] != 200]
                problem = f"requests failed or not sent: {failed}" if failed else None
            else:
                problem = CHECKS[spec["check"]](run, **args)
            label = spec["check"] if not args else f"{spec['check']} {args}"
            results.append(
                EvaluationOutput(
                    score=0.0 if problem else 1.0,
                    test_pass=problem is None,
                    reason=f"FAIL {label}: {problem}" if problem else f"ok {label}",
                )
            )
        return results


# --- LLM judge ------------------------------------------------------------------------------


class RubricJudge(Evaluator[list, str]):
    """Judges the conversation against the case's rubric, with a cheap judge model.

    A new judge model per case, closed on the event loop that used it (see close_model).
    """

    def __init__(self, judge_model: Callable[[], Model]) -> None:
        super().__init__()
        self.judge_model = judge_model
        self.aggregator = self._aggregate_dropping_na

    def evaluate(self, evaluation_case: EvaluationData[list, str]) -> list[EvaluationOutput]:
        raise NotImplementedError("RubricJudge runs async only; Experiment calls evaluate_async.")

    async def evaluate_async(
        self, evaluation_case: EvaluationData[list, str]
    ) -> list[EvaluationOutput]:
        rubric = (evaluation_case.metadata or {}).get("rubric")
        if not rubric:
            return [
                EvaluationOutput(
                    score=0.0, test_pass=True, reason="no rubric", label=NOT_APPLICABLE
                )
            ]
        model = self.judge_model()
        try:
            judge = OutputEvaluator(rubric=rubric, model=model, include_inputs=True)
            return await judge.evaluate_async(evaluation_case)
        finally:
            await close_model(model)
