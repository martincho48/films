"""Evaluation of the agent's decisions against the golden set (tests/golden.jsonl).

Sends every question to Gemini and compares the returned call with the expected one.
BigQuery is not called, only the model's decision is checked. Costs tokens
(~0.5 haléře per question), so run it when tools.py, agent.py or the golden set changes.

    python tests/eval_agent.py               # all cases
    python tests/eval_agent.py --repeat 3    # each case 3x, measures stability
    python tests/eval_agent.py --tag cinema  # only cases with this tag

Golden line: {"id", "q", "expect" | "any_of", "ignore"?, "tags", "note"?}
  expect   expected arguments of search_movies, null = no tool call
  any_of   list of acceptable argument sets (ambiguous questions)
  ignore   arguments that may have any value in this case
Anything else the model sends is an error, except default values.

Every run is saved to tests/runs/<timestamp>.json for comparison.
"""

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

from agent import MODEL, decide  # noqa: E402
from tools import TOOLS  # noqa: E402

GOLDEN = Path(__file__).with_name("golden.jsonl")
RUNS = Path(__file__).with_name("runs")
TODAY = date(2026, 10, 27)  # the golden set's relative dates ('next week') are written for this day
TOOL = "search_movies"
SCHEMA = set(TOOLS[TOOL][1]["parameters"]["properties"])
DEFAULTS = {"country": "CZ", "limit": 20, "new_only": False}


def normalize(args: dict) -> dict:
    """Drop None and default values, make values comparable."""
    out = {}
    for k, v in args.items():
        if v is None or DEFAULTS.get(k, object()) == v:
            continue
        if isinstance(v, list):
            v = sorted(set(v))
        elif k == "person":
            v = str(v).strip().lower()
        elif isinstance(v, float) and v.is_integer():
            v = int(v)
        out[k] = v
    # sort_by equal to the function's own default means the same as leaving it out
    default_sort = "premiere" if out.get("cinema") == "upcoming" else "popularity"
    if out.get("sort_by") == default_sort:
        del out["sort_by"]
    return out


def diff(expected: dict, actual: dict, ignore: set) -> list[tuple[str, str]]:
    """List of (param, kind) with kind in missing / wrong / extra."""
    errors = []
    for k in expected.keys() | actual.keys():
        if k in ignore:
            continue
        if k not in actual:
            errors.append((k, "missing"))
        elif k not in expected:
            errors.append((k, "extra"))
        elif expected[k] != actual[k]:
            errors.append((k, "wrong"))
    return sorted(errors)


def evaluate(case: dict) -> dict:
    t0 = time.perf_counter()
    d = decide(case["q"], today=TODAY)
    latency = time.perf_counter() - t0

    candidates = case["any_of"] if "any_of" in case else [case["expect"]]
    should_call = candidates[0] is not None
    called = d.tool is not None
    unknown = sorted(set(d.args) - SCHEMA) if called else []
    actual = normalize(d.args) if called else {}

    if not should_call or not called or d.tool != TOOL:
        errors = [] if should_call == called and (not called or d.tool == TOOL) else [("_call", "wrong")]
        best = candidates[0] or {}
    else:
        ignore = set(case.get("ignore", []))
        options = [(diff(normalize(c), actual, ignore), c) for c in candidates]
        errors, best = min(options, key=lambda o: len(o[0]))

    return {
        "id": case["id"], "q": case["q"], "tags": case.get("tags", []),
        "should_call": should_call, "called": called, "tool": d.tool,
        "args": d.args, "expected": best, "errors": errors, "unknown_params": unknown,
        "ok": not errors and not unknown,
        "text": d.text, "input_tokens": d.input_tokens, "output_tokens": d.output_tokens,
        "thinking_tokens": d.thinking_tokens, "cost_usd": d.cost_usd, "latency_s": round(latency, 2),
    }


def report(results: list[dict], repeat: int) -> dict:
    n = len(results)
    ok = sum(r["ok"] for r in results)
    call_ok = sum(r["should_call"] == r["called"] for r in results)
    tp = sum(r["should_call"] and r["called"] for r in results)
    fp = sum(not r["should_call"] and r["called"] for r in results)
    fn = sum(r["should_call"] and not r["called"] for r in results)

    # per parameter: how often it was expected and right, and its errors by kind
    per_param = defaultdict(Counter)
    for r in results:
        if not (r["should_call"] and r["called"]):
            continue
        wrong = {k for k, _ in r["errors"]}
        for k in normalize(r["expected"] or {}):
            per_param[k]["expected"] += 1
            per_param[k]["right"] += k not in wrong
        for k, kind in r["errors"]:
            per_param[k][kind] += 1

    # stability: the same question answered differently across repeats
    unstable = []
    if repeat > 1:
        by_id = defaultdict(set)
        for r in results:
            by_id[r["id"]].add(json.dumps(normalize(r["args"]) if r["called"] else None, sort_keys=True))
        unstable = sorted(i for i, answers in by_id.items() if len(answers) > 1)

    cost = sum(r["cost_usd"] for r in results)
    summary = {
        "model": MODEL, "cases": n // repeat, "repeat": repeat,
        "exact_match": ok, "exact_match_pct": round(100 * ok / n, 1),
        "call_decision": call_ok, "call_precision": round(tp / (tp + fp), 3) if tp + fp else None,
        "call_recall": round(tp / (tp + fn), 3) if tp + fn else None,
        "unknown_params": sum(bool(r["unknown_params"]) for r in results),
        "unstable": unstable,
        "per_param": {k: dict(v) for k, v in sorted(per_param.items())},
        "cost_usd": round(cost, 6), "avg_input_tokens": round(sum(r["input_tokens"] for r in results) / n),
        "avg_latency_s": round(sum(r["latency_s"] for r in results) / n, 2),
    }

    print(f"\nmodel {MODEL}, {n // repeat} případů × {repeat}")
    print(f"přesná shoda      {ok}/{n} ({summary['exact_match_pct']} %)")
    print(f"volat / nevolat   {call_ok}/{n}  (precision {summary['call_precision']}, recall {summary['call_recall']})")
    print(f"vymyšlené param.  {summary['unknown_params']}")
    if repeat > 1:
        print(f"nestabilní        {len(unstable)} {unstable}")
    print("\nparametr          správně  chybí  špatně  navíc")
    for k, c in summary["per_param"].items():
        print(f"{k:16} {c.get('right', 0):>3}/{c.get('expected', 0):<4} {c.get('missing', 0):>5} {c.get('wrong', 0):>7} {c.get('extra', 0):>6}")
    print(f"\ncena ≈ ${cost:.5f}, průměrně {summary['avg_input_tokens']} vstupních tokenů, {summary['avg_latency_s']} s/dotaz")

    failed = [r for r in results if not r["ok"]]
    if failed:
        print("\nCHYBY")
        for r in failed:
            got = f"{r['tool']}({r['args']})" if r["called"] else f"nic: {(r['text'] or '').strip()[:60]!r}"
            print(f"  {r['id']:10} {r['q']}")
            print(f"             čekal {r['expected']}")
            print(f"             dostal {got}")
            print(f"             {r['errors'] + [(p, 'unknown') for p in r['unknown_params']]}")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--tag")
    a = ap.parse_args()

    cases = [json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines() if line.strip()]
    if a.tag:
        cases = [c for c in cases if a.tag in c.get("tags", [])]

    results = []
    for i in range(a.repeat):
        for c in cases:
            r = evaluate(c)
            results.append(r)
            print(f"{'OK ' if r['ok'] else 'ERR'} {r['id']:10} {r['q']}")

    summary = report(results, a.repeat)
    RUNS.mkdir(exist_ok=True)
    out = RUNS / f"{datetime.now():%Y-%m-%dT%H-%M-%S}.json"
    out.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=1, default=str),
                   encoding="utf-8")
    print(f"\nuloženo {out.relative_to(Path.cwd()) if out.is_relative_to(Path.cwd()) else out}")


if __name__ == "__main__":
    main()
