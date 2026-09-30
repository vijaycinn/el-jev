"""Live functional eval: routing accuracy, latency, gate behaviour and hook end-to-end.

This is a smoke/regression check that el-jev works as designed against your Foundry
deployment. It is not a calibration study: the prompts are synthetic, so use the
capture -> label -> calibrate sequence for held-out risk claims.

Requires a running daemon (`python -m eljev daemon start`) and a configured endpoint.
Exit codes: 0 all checks pass, 1 a check failed, 2 prerequisites missing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "hooks"))

import eljev_pre_turn as hook  # noqa: E402
from eljev import config  # noqa: E402
from eljev.client import EljevClient  # noqa: E402
from eljev.engines.cohere import CohereClient  # noqa: E402
from eljev.engines.systemone import SystemOneEngine  # noqa: E402

DEFAULT_REPORTS = REPO / "eval" / "reports"

LABELLED_INTENTS: dict[str, list[str]] = {
    "code_modification": [
        "Refactor the retry loop in cohere.py to use exponential backoff",
        "Add a --json flag to the status command and update the parser",
        "Fix the off-by-one bug in the pagination helper",
        "Write a new function that validates the calibration file schema",
        "Rename the variable userId to userID across the users module",
        "Implement caching for the token minting call in the client",
    ],
    "review_audit": [
        "Review this pull request for security vulnerabilities before I merge",
        "Do a code review of the daemon changes and flag any bugs",
        "Audit the auth flow for credential leakage risks",
        "Critique my PR diff for correctness issues",
        "Perform a security audit of the MCP server input handling",
        "Give me an adversarial review of the verdict gate logic",
    ],
    "investigation_search": [
        "Find where the pidfile is written in the codebase",
        "Search the repo for all callers of apply_gate",
        "Which files reference the ELJEV_COHERE_ENDPOINT variable",
        "Look through the daemon logs for the last timeout error",
        "Locate the definition of the INTENT_CRITERIA dictionary",
        "Explore how the hook discovers the daemon port",
    ],
    "execution_testing": [
        "Run the unit test suite and tell me what fails",
        "Build the package and install it into the venv",
        "Execute the live Foundry tests with ELJEV_LIVE_TESTS set",
        "Deploy the container app to the staging environment",
        "Start the daemon and run the latency benchmark script",
        "Run npm install and then the lint command",
    ],
    "advisory_explanation": [
        "Explain how softmax temperature affects the confidence scores",
        "What is the difference between a reranker and a classifier",
        "Should we use a cross-encoder or a bi-encoder for routing",
        "Describe the tradeoffs of fail-open versus fail-closed hooks",
        "Why does Jev use calibrated probabilities instead of LLM reasoning",
        "Give me high-level architecture advice for a low latency decision sidecar",
    ],
}

INTENT_QUESTION = {"id": "intent_choice", "kind": "choice", "criteria": hook.INTENT_CRITERIA}

NOUL_STATE = "Incident: Primary SQL database cluster has been unreachable for 15 minutes."
NOUL_QUESTION = {
    "id": "urgency_1",
    "kind": "noul",
    "instructions": "Is this a high severity production incident?",
    "criteria": {
        "true": "Critical database downtime impacting availability",
        "false": "Low severity non-critical informational event",
    },
}

SCREEN_CRITERION = "which item is most urgent to act on today"
SCREEN_URGENT_ID = "item_49"
SCREEN_CANDIDATES = [
    {"id": f"item_{index:02d}", "text": text}
    for index, text in enumerate(
        [f"Routine backlog task {index}: tidy docs section {index}" for index in range(49)]
        + ["Production outage: payments API returning 500 for all enterprise customers"]
    )
]

HOOK_PROMPTS = [
    ("Search the repo for all callers of apply_gate", True),
    ("Run the unit test suite and tell me what fails", True),
    ("hi there", False),
    ("Explain how softmax temperature affects the confidence scores", True),
    ("Review this pull request for security vulnerabilities before I merge", True),
]

# Illustrative only: exercises the calibrated code path, not a fitted operating point.
ILLUSTRATIVE_CALIBRATION = {
    "schema": "eljev.eval.calibration/1",
    "calibration_version": "functional-eval-illustrative",
    "kinds": {
        "choice": {
            "calibration_version": "functional-eval-illustrative-choice",
            "temperature": 0.05,
            "threshold": 0.9,
            "margin_threshold": 0.8,
        }
    },
}

RISK_COVERAGE_POINTS = [(0.0, 0.0), (0.8, 0.5), (0.9, 0.7), (0.95, 0.85), (0.99, 0.95)]


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _latency(samples: list[float], budget_ms: float) -> dict[str, Any]:
    return {
        "n": len(samples),
        "p50": round(_percentile(samples, 0.5), 1),
        "p95": round(_percentile(samples, 0.95), 1),
        "max": round(max(samples), 1),
        f"over_{int(budget_ms)}ms": sum(1 for sample in samples if sample > budget_ms),
    }


def _timed(function: Callable[..., Any], *args: Any, **kwargs: Any) -> tuple[Any, float]:
    started = time.perf_counter()
    value = function(*args, **kwargs)
    return value, (time.perf_counter() - started) * 1000.0


def _pick(record: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {key: record.get(key) for key in keys}


def run_live_unit_tests() -> dict[str, Any]:
    environment = dict(os.environ, ELJEV_LIVE_TESTS="1", PYTHONPATH=str(REPO))
    process = subprocess.run(
        [sys.executable, "-m", "unittest", "tests.test_live_foundry", "-v"],
        cwd=REPO,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )
    lines = process.stderr.strip().splitlines()
    return {"passed": process.returncode == 0, "summary": " ".join(lines[-3:])}


def eval_intent_routing(client: EljevClient) -> dict[str, Any]:
    client.decide("warm up the connection before measuring", INTENT_QUESTION)
    rows: list[dict[str, Any]] = []
    for label, prompts in LABELLED_INTENTS.items():
        for prompt in prompts:
            record, elapsed = _timed(client.decide, prompt, INTENT_QUESTION)
            rows.append(
                {
                    "label": label,
                    "prompt": prompt,
                    "choice": record.get("choice"),
                    "status": record.get("status"),
                    "exit_code": record.get("exit_code"),
                    "p_top": record.get("calibrated_probability"),
                    "margin": record.get("margin_calibrated"),
                    "correct": record.get("choice") == label,
                    "ms": round(elapsed, 1),
                }
            )

    correct = sum(row["correct"] for row in rows)
    right = [row["p_top"] for row in rows if row["correct"] and row["p_top"] is not None]
    wrong = [row["p_top"] for row in rows if not row["correct"] and row["p_top"] is not None]
    risk_coverage = []
    for threshold, margin_threshold in RISK_COVERAGE_POINTS:
        selected = [
            row
            for row in rows
            if row["p_top"] is not None
            and row["p_top"] >= threshold
            and (row["margin"] or 0.0) >= margin_threshold
        ]
        accuracy = sum(row["correct"] for row in selected) / len(selected) if selected else None
        risk_coverage.append(
            {
                "threshold": threshold,
                "margin_threshold": margin_threshold,
                "coverage": f"{len(selected)}/{len(rows)}",
                "selective_accuracy": None if accuracy is None else round(accuracy, 3),
            }
        )
    return {
        "accuracy": correct / len(rows),
        "correct": f"{correct}/{len(rows)}",
        "per_intent": {
            label: f"{sum(row['correct'] for row in rows if row['label'] == label)}/{len(prompts)}"
            for label, prompts in LABELLED_INTENTS.items()
        },
        "errors": sum(
            1 for row in rows if row["status"] not in {"needs_review", "selected", "abstain_tie"}
        ),
        "exit0_under_current_policy": sum(1 for row in rows if row["exit_code"] == 0),
        "mean_p_top_correct": round(statistics.mean(right), 3) if right else None,
        "mean_p_top_wrong": round(statistics.mean(wrong), 3) if wrong else None,
        "misroutes": [
            _pick(row, "prompt", "label", "choice", "p_top", "margin") for row in rows if not row["correct"]
        ],
        "risk_coverage_display_softmax": risk_coverage,
        "latencies_ms": [row["ms"] for row in rows],
        "rows": rows,
    }


def eval_noul_and_screen(client: EljevClient, iterations: int) -> dict[str, Any]:
    noul_latency, noul_correct = [], 0
    for _ in range(iterations):
        record, elapsed = _timed(client.decide, NOUL_STATE, NOUL_QUESTION)
        noul_latency.append(elapsed)
        noul_correct += record.get("choice") == "true"

    screen_latency, screen_correct = [], 0
    for _ in range(iterations):
        record, elapsed = _timed(client.screen, SCREEN_CRITERION, SCREEN_CANDIDATES, top_n=10)
        screen_latency.append(elapsed)
        screen_correct += record.get("choice") == SCREEN_URGENT_ID
    return {
        "noul": {"latencies_ms": noul_latency, "correct": noul_correct, "n": iterations},
        "screen_50": {"latencies_ms": screen_latency, "correct": screen_correct, "n": iterations},
    }


def eval_gate() -> dict[str, Any]:
    saved = {key: os.environ.get(key) for key in ("ELJEV_CALIBRATION_PATH", "ELJEV_COVERAGE_POLICY")}
    results: dict[str, Any] = {}
    with tempfile.TemporaryDirectory() as temporary:
        calibration_path = Path(temporary) / "calibration.json"
        calibration_path.write_text(json.dumps(ILLUSTRATIVE_CALIBRATION), encoding="utf-8")
        os.environ["ELJEV_CALIBRATION_PATH"] = str(calibration_path)
        os.environ["ELJEV_COVERAGE_POLICY"] = "calibrated"
        try:
            live = SystemOneEngine(
                cohere=CohereClient(endpoint=config.cohere_endpoint(), deployment=config.cohere_deployment())
            )
            clear = live.decide("Run the unit test suite and tell me what fails", INTENT_QUESTION)
            ambiguous = live.decide("Check the hook implementation", INTENT_QUESTION)
            missing = live.decide(NOUL_STATE, NOUL_QUESTION)
            unreachable = SystemOneEngine(
                cohere=CohereClient(
                    endpoint="https://127.0.0.1:9",
                    deployment=config.cohere_deployment(),
                    timeout_ms=400,
                    token_provider=lambda: "functional-eval-dummy-token",
                )
            )
            failed, failed_ms = _timed(unreachable.decide, "Run the unit test suite", INTENT_QUESTION)
        finally:
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    fields = ("choice", "status", "exit_code", "calibrated_probability", "margin_calibrated", "calibration_version", "notes")
    results["calibrated_clear"] = _pick(clear, *fields)
    results["calibrated_ambiguous"] = _pick(ambiguous, *fields)
    results["calibrated_missing_kind"] = _pick(missing, "choice", "status", "exit_code", "calibration_version", "notes")
    results["engine_error"] = {
        **_pick(failed, "choice", "status", "exit_code", "error_kind", "probabilities"),
        "ms": round(failed_ms, 1),
    }
    results["checks"] = {
        "clear prompt -> selected, exit 0": clear.get("status") == "selected" and clear.get("exit_code") == 0,
        "ambiguous prompt -> exit 2": ambiguous.get("exit_code") == 2,
        "uncalibrated kind -> fail closed": missing.get("exit_code") == 2
        and missing.get("calibration_version") == "none",
        "engine error -> null choice, non-zero exit": failed.get("choice") is None
        and failed.get("exit_code") not in (0, None),
    }
    return results


def eval_hook() -> dict[str, Any]:
    runs = []
    for prompt, expect_advisory in HOOK_PROMPTS:
        started = time.perf_counter()
        process = subprocess.run(
            [sys.executable, str(REPO / "hooks" / "eljev_pre_turn.py")],
            input=json.dumps({"prompt": prompt, "sessionId": "functional-eval"}),
            capture_output=True,
            text=True,
            timeout=15,
            env=dict(os.environ, EL_JEV="ON"),
        )
        wall_ms = (time.perf_counter() - started) * 1000.0
        output = json.loads(process.stdout or "{}")
        injected = "modifiedTransformedPrompt" in output
        runs.append(
            {
                "prompt": prompt,
                "expect_advisory": expect_advisory,
                "injected": injected,
                "as_expected": injected == expect_advisory,
                "wall_ms": round(wall_ms, 1),
            }
        )
    return {"runs": runs, "all_as_expected": all(run["as_expected"] for run in runs)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, help="Report path (default: eval/reports/functional-eval-<utc>.json)")
    parser.add_argument("--iterations", type=int, default=20, help="Repetitions for the noul and screen latency runs")
    parser.add_argument("--min-accuracy", type=float, default=0.9, help="Minimum intent-routing accuracy to pass")
    parser.add_argument("--budget-ms", type=float, default=500.0, help="Hook decision budget used for latency counts")
    parser.add_argument("--skip-unit-tests", action="store_true", help="Skip tests/test_live_foundry.py")
    parser.add_argument("--skip-hook", action="store_true", help="Skip the hook subprocess runs")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not config.cohere_endpoint():
        print("No Cohere endpoint configured. Run: python -m eljev configure --endpoint <url>", file=sys.stderr)
        return 2
    client = EljevClient(timeout_s=10)
    health = client.health()
    if health.get("status") != "ok":
        print("Daemon is not healthy. Run: python -m eljev daemon start", file=sys.stderr)
        return 2
    report: dict[str, Any] = {
        "schema": "eljev.eval.functional/1",
        "run_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "health": _pick(health, "status", "version", "engines", "auth", "warm", "coverage_policy", "calibration_version"),
    }
    checks: dict[str, bool] = {}
    print(f"[health] {json.dumps(report['health'])}")

    if not args.skip_unit_tests:
        report["live_unit_tests"] = run_live_unit_tests()
        checks["live Foundry unit tests pass"] = report["live_unit_tests"]["passed"]
        print(f"[unit] {report['live_unit_tests']['summary']}")

    routing = eval_intent_routing(client)
    report["intent_routing"] = routing
    checks[f"intent accuracy >= {args.min_accuracy:.0%}"] = routing["accuracy"] >= args.min_accuracy
    checks["no engine errors during routing"] = routing["errors"] == 0
    if health.get("coverage_policy") == "always_abstain_v0":
        checks["default policy never exits 0"] = routing["exit0_under_current_policy"] == 0
    print(f"[routing] {routing['correct']} per-intent {routing['per_intent']} errors {routing['errors']}")

    shapes = eval_noul_and_screen(client, args.iterations)
    report["latency_ms"] = {
        "choice_5way": _latency(routing.pop("latencies_ms"), args.budget_ms),
        "noul": {**_latency(shapes["noul"]["latencies_ms"], args.budget_ms), "correct": f"{shapes['noul']['correct']}/{args.iterations}"},
        "screen_50": {**_latency(shapes["screen_50"]["latencies_ms"], args.budget_ms), "correct": f"{shapes['screen_50']['correct']}/{args.iterations}"},
    }
    checks["noul picks true every time"] = shapes["noul"]["correct"] == args.iterations
    checks["screen ranks the outage first every time"] = shapes["screen_50"]["correct"] == args.iterations
    print(f"[latency] {json.dumps(report['latency_ms'])}")

    report["gate"] = eval_gate()
    checks.update(report["gate"]["checks"])
    print(f"[gate] {json.dumps(report['gate']['checks'])}")

    if not args.skip_hook:
        hook_result = eval_hook()
        hook_result["wall_ms"] = _latency([run["wall_ms"] for run in hook_result["runs"]], args.budget_ms)
        report["hook_e2e"] = hook_result
        checks["hook injects advisories and skips short prompts"] = hook_result["all_as_expected"]
        print(f"[hook] as_expected={hook_result['all_as_expected']} wall_ms={json.dumps(hook_result['wall_ms'])}")

    report["checks"] = checks
    report["passed"] = all(checks.values())
    out = args.out or DEFAULT_REPORTS / f"functional-eval-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    for name, passed in checks.items():
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")
    print(f"{'PASS' if report['passed'] else 'FAIL'} - report: {out}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
