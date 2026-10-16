"""Run the evals: python -m evals [--case NAME ...]

Runs every case against the real model with fake supplies tools, prints a summary, writes
evals/results/latest.json, and exits 1 if any case failed. Needs ANTHROPIC_API_KEY (or .env).
Judge model: EVAL_JUDGE_MODEL_ID (see EvalSettings).
"""

import argparse
import logging
import os
import sys
from collections import defaultdict
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from strands.models.anthropic import AnthropicModel
from strands_evals import Case, Experiment
from strands_evals.types import EnvironmentState

from evals.cases import CASES
from evals.evaluators import RUN_STATE, RubricJudge, ScenarioChecks
from evals.harness import play, transcript
from supplies_agent.agent.settings import AgentSettings

RESULTS = Path(__file__).parent / "results" / "latest.json"


class EvalSettings(BaseSettings):
    """Read from env vars prefixed EVAL_ (and a local .env file)."""

    model_config = SettingsConfigDict(env_prefix="EVAL_", env_file=".env", extra="ignore")

    judge_model_id: str = "claude-haiku-5-5"  # cheaper than the agent's model; judging is simple


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m evals", description=__doc__.splitlines()[0])
    parser.add_argument("--case", action="append", help="run only cases whose name contains this")
    args = parser.parse_args()

    # The services' INFO logs would drown the summary.
    logging.basicConfig(level=logging.WARNING)

    settings = AgentSettings()
    api_key = settings.anthropic_api_key.get_secret_value() if settings.anthropic_api_key else None
    judge_model_id = EvalSettings().judge_model_id

    def judge_model() -> AnthropicModel:
        client_args = {"api_key": api_key} if api_key else {}
        return AnthropicModel(client_args=client_args, model_id=judge_model_id, max_tokens=1024)

    cases = [c for c in CASES if not args.case or any(name in c.name for name in args.case)]
    if not cases:
        print(f"No case matches {args.case}. Cases: {', '.join(c.name for c in CASES)}")
        return 2

    def task(case: Case) -> dict:
        run = play(case.input, case.metadata["setup"], settings)
        return {
            "output": transcript(run),
            "trajectory": run["trajectory"],
            "environment_state": [EnvironmentState(name=RUN_STATE, state=run)],
        }

    experiment = Experiment[list, str](
        cases=cases, evaluators=[ScenarioChecks(), RubricJudge(judge_model)]
    )
    report = experiment.run_evaluations(task)
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    report.to_file(str(RESULTS))

    # One line per case: it passes only if every evaluator passed it.
    passed: dict[str, bool] = defaultdict(lambda: True)
    failures: dict[str, list[str]] = defaultdict(list)
    for row, ok, reason in zip(report.cases, report.test_passes, report.reasons, strict=True):
        passed[row["name"]] &= ok
        if not ok:
            failures[row["name"]].append(f"{row['evaluator']}: {reason}")

    print()
    for case in cases:
        print(f"{'PASS' if passed[case.name] else 'FAIL'}  {case.name}")
        for failure in failures[case.name]:
            print(f"        {failure}")
    total = sum(passed[c.name] for c in cases)
    print(f"\n{total}/{len(cases)} cases passed. Results: {os.path.relpath(RESULTS)}")
    return 0 if total == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
