"""Turn an eval results file into a self-contained HTML report.

    python scripts/eval_report.py [RESULTS] [-o OUTPUT]

RESULTS defaults to evals/results/latest.json (written by `python -m evals`); OUTPUT defaults to
the same path with .html. The results hold one row per (case, evaluator); the report shows one
entry per case, which passes only if every evaluator passed it, grouped by the case name's prefix
(tool-, approval-, scope-, failure-, facts-). Standard library only.
"""

import argparse
import json
from datetime import datetime
from html import escape
from pathlib import Path

DEFAULT_RESULTS = Path(__file__).parent.parent / "evals" / "results" / "latest.json"

STYLE = """
:root { --pass: #1a7f37; --fail: #cf222e; --muted: #59636e; --line: #d1d9e0; --bg: #f6f8fa; }
body { font: 14px/1.5 system-ui, sans-serif; margin: 0 auto; max-width: 64rem; padding: 2rem 1rem;
       color: #1f2328; background: #fff; }
h1 { margin: 0 0 .25rem; } h2 { margin: 2rem 0 .5rem; font-size: 1.1rem; }
.muted { color: var(--muted); }
.summary { display: flex; gap: 1rem; flex-wrap: wrap; margin: 1rem 0; }
.summary div { border: 1px solid var(--line); border-radius: 6px; padding: .5rem 1rem; }
.summary b { display: block; font-size: 1.4rem; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; padding: .35rem .6rem; border-bottom: 1px solid var(--line);
         vertical-align: top; }
.badge { display: inline-block; min-width: 3rem; text-align: center; border-radius: 4px;
         color: #fff; font-weight: 600; font-size: 12px; padding: 0 .4rem; }
.pass { background: var(--pass); } .fail { background: var(--fail); }
details { border: 1px solid var(--line); border-radius: 6px; margin: .5rem 0; }
summary { cursor: pointer; padding: .5rem .75rem; display: flex; gap: .75rem; align-items: center; }
details[open] summary { border-bottom: 1px solid var(--line); background: var(--bg); }
.body { padding: .75rem; }
.label { font-weight: 600; margin: .75rem 0 .25rem; }
pre { background: var(--bg); padding: .5rem .75rem; border-radius: 6px; white-space: pre-wrap;
      word-break: break-word; margin: 0; }
"""


def load_rows(path: Path) -> list[dict]:
    """One dict per (case, evaluator) row, with that evaluator's verdict merged in."""
    report = json.loads(path.read_text())
    return [
        {**case, "test_pass": ok, "score": score, "reason": reason}
        for case, ok, score, reason in zip(
            report["cases"], report["test_passes"], report["scores"], report["reasons"], strict=True
        )
    ]


def by_case(rows: list[dict]) -> dict[str, list[dict]]:
    cases: dict[str, list[dict]] = {}
    for row in rows:
        cases.setdefault(row["name"], []).append(row)
    return cases


def group_of(name: str) -> str:
    return name.split("-", 1)[0]


def states(row: dict, key: str) -> dict:
    return {s["name"]: s["state"] for s in row.get(key) or []}


def block(label: str, value) -> str:
    if value in (None, "", [], {}):
        return ""
    text = value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)
    return f'<div class="label">{escape(label)}</div><pre>{escape(text)}</pre>'


def badge(ok: bool) -> str:
    return f'<span class="badge {"pass" if ok else "fail"}">{"PASS" if ok else "FAIL"}</span>'


def evaluator_rows(rows: list[dict]) -> str:
    """One table row per evaluator; a state check shows expected and actual values."""
    out = []
    for row in rows:
        name = row["evaluator"]
        expected = states(row, "expected_environment_state")
        if name in expected:
            actual = states(row, "actual_environment_state").get(name)
            detail = f"expected {json.dumps(expected[name])}, got {json.dumps(actual)}"
        else:
            detail = row["reason"] or ""
        out.append(
            f"<tr><td>{badge(row['test_pass'])}</td><td>{escape(name)}</td>"
            f"<td>{row['score']:.2f}</td><td>{escape(detail)}</td></tr>"
        )
    return "".join(out)


def case_html(name: str, rows: list[dict]) -> str:
    first = rows[0]
    ok = all(r["test_pass"] for r in rows)
    actual_state = states(first, "actual_environment_state")
    return f"""
<details{" open" if not ok else ""}>
  <summary>{badge(ok)} <b>{escape(name)}</b>
    <span class="muted">{escape(", ".join(r["evaluator"] for r in rows))}</span></summary>
  <div class="body">
    <table><tr><th></th><th>Evaluator</th><th>Score</th><th>Detail</th></tr>
      {evaluator_rows(rows)}</table>
    {block("Input", first.get("input"))}
    {block("Case setup (metadata)", first.get("metadata"))}
    {block("Expected trajectory", first.get("expected_trajectory"))}
    {block("Actual trajectory", first.get("actual_trajectory"))}
    {block("Expected output (given to the judge)", first.get("expected_output"))}
    {block("Actual output", first.get("actual_output"))}
    {block("End state", actual_state)}
  </div>
</details>"""


def render(rows: list[dict], source: Path) -> str:
    cases = by_case(rows)
    groups: dict[str, dict[str, list[dict]]] = {}
    for name, case_rows in cases.items():
        groups.setdefault(group_of(name), {})[name] = case_rows

    def passed(cs: dict[str, list[dict]]) -> int:
        return sum(all(r["test_pass"] for r in rs) for rs in cs.values())

    total, ok = len(cases), passed(cases)
    run_at = datetime.fromtimestamp(source.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
    group_table = "".join(
        f"<tr><td>{badge(passed(cs) == len(cs))}</td><td>{escape(g)}</td>"
        f"<td>{passed(cs)}/{len(cs)}</td></tr>"
        for g, cs in groups.items()
    )
    sections = "".join(
        f"<h2>{escape(g)} <span class='muted'>{passed(cs)}/{len(cs)}</span></h2>"
        + "".join(case_html(n, rs) for n, rs in cs.items())
        for g, cs in groups.items()
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Supplies agent evals</title><style>{STYLE}</style></head>
<body>
<h1>Supplies agent evals</h1>
<div class="muted">{escape(source.name)}, written {run_at}</div>
<div class="summary">
  <div><b>{ok}/{total}</b>cases passed</div>
  <div><b>{sum(r["test_pass"] for r in rows)}/{len(rows)}</b>evaluator checks passed</div>
  <div><b>{len(groups)}</b>groups</div>
</div>
<table><tr><th></th><th>Group</th><th>Cases passed</th></tr>{group_table}</table>
{sections}
</body></html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("results", nargs="?", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("-o", "--output", type=Path, help="default: RESULTS with .html")
    args = parser.parse_args()
    output = args.output or args.results.with_suffix(".html")
    output.write_text(render(load_rows(args.results), args.results))
    print(f"Report: {output}")


if __name__ == "__main__":
    main()
