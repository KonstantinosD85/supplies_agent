"""Run the evals: python -m evals [--case NAME ...]

Runs every group's cases against the real model with fake supplies tools, prints one line per
case, writes evals/results/latest.json, and exits 1 if any case failed. Needs ANTHROPIC_API_KEY
(or .env). Judge model: EVAL_JUDGE_MODEL_ID (see EvalSettings).
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from strands.models.anthropic import AnthropicModel
from strands_evals import Experiment
from strands_evals.types.evaluation_report import EvaluationReport

from evals.cases import groups
from evals.task import make_task
from supplies_agent.agent.model import build_model, close_model
from supplies_agent.agent.settings import AgentSettings

RESULTS = Path(__file__).parent / "results" / "latest.json"


class EvalSettings(BaseSettings):
    """Read from env vars prefixed EVAL_ (and a local .env file)."""

    model_config = SettingsConfigDict(env_prefix="EVAL_", env_file=".env", extra="ignore")

    judge_model_id: str = "claude-haiku-5-5"  # cheaper than the agent's model; judging is simple


async def run(selected: list[str] | None) -> EvaluationReport | None:
    """Run every group (or only the selected cases) in one event loop, and merge the reports."""
    settings = AgentSettings()
    api_key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
    judge = AnthropicModel(
        client_args={"api_key": api_key} if api_key else {},
        model_id=EvalSettings().judge_model_id,
        max_tokens=1024,
    )
    task = make_task(lambda: build_model(settings))
    reports = []
    try:
        for name, (cases, evaluators) in groups(judge).items():
            cases = [c for c in cases if not selected or any(s in c.name for s in selected)]
            if cases:
                print(f"Running {name} ({len(cases)} cases)...")
                experiment = Experiment[str, str](cases=cases, evaluators=evaluators)
                reports.append(await experiment.run_evaluations_async(task, max_workers=1))
    finally:
        await close_model(judge)  # on the loop that used it (see close_model)
    return EvaluationReport.flatten(reports) if reports else None


def explain(row: dict, reason: str) -> str:
    """The evaluator's reason, plus expected and actual values for a state check."""
    name = row["evaluator"]
    expected = {s["name"]: s["state"] for s in row.get("expected_environment_state") or []}
    actual = {s["name"]: s["state"] for s in row.get("actual_environment_state") or []}
    if name in expected:
        return f"{name}: expected {expected[name]!r}, got {actual.get(name)!r}"
    return f"{name}: {reason}"


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m evals", description=__doc__.splitlines()[0])
    parser.add_argument("--case", action="append", help="run only cases whose name contains this")
    args = parser.parse_args()

    # INFO logs would drown the summary. force: strands_evals installs its own INFO handler.
    logging.basicConfig(level=logging.WARNING, force=True)

    report = asyncio.run(run(args.case))
    if report is None:
        print(f"No case matches {args.case}.")
        return 2
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    report.to_file(str(RESULTS))

    # One line per case: it passes only if every evaluator passed it.
    results: dict[str, list[str]] = {}
    for row, ok, reason in zip(report.cases, report.test_passes, report.reasons, strict=True):
        failures = results.setdefault(row["name"], [])
        if not ok:
            failures.append(explain(row, reason))

    print()
    for name, failures in results.items():
        print(f"{'FAIL' if failures else 'PASS'}  {name}")
        for failure in failures:
            print(f"        {failure}")
    passed = sum(not failures for failures in results.values())
    print(f"\n{passed}/{len(results)} cases passed. Results: {os.path.relpath(RESULTS)}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
