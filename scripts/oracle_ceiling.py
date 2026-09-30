"""S1 oracle-ceiling kill-gate harness for el-jev.

The harness compares the current baseline path with the same task after a
known-correct verdict is injected through the host's real trigger. The host
adapter is deliberately explicit: it receives one JSON request per line and
must perform the actual ``userPromptTransformed``/pre-turn injection for the
oracle arm. The harness never claims to be a Copilot runtime.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from queue import Empty, Queue
from typing import Any


ORACLE_SCHEMA = "eljev.oracle/1"
REQUEST_SCHEMA = "eljev.oracle.request/1"
RESULT_SCHEMA = "eljev.oracle.result/1"
MIN_PAIRS = 10
MATERIAL_RELATIVE_REDUCTION = 0.10
MATERIAL_TURN_REDUCTION = 1.0


def _usage() -> str:
    return (
        "Usage:\n"
        "  python scripts\\oracle_ceiling.py --input decisions.jsonl --runner runner.py [--report report.json]\n"
        "  python scripts\\oracle_ceiling.py --demo clear-win\n"
        "  python scripts\\oracle_ceiling.py --demo no-difference\n"
    )


def _parse_args(argv: list[str]) -> dict[str, str | None]:
    values: dict[str, str | None] = {
        "input": None,
        "runner": None,
        "report": None,
        "demo": None,
    }
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in {"-h", "--help"}:
            print(_usage())
            raise SystemExit(0)
        if item == "--demo":
            if index + 1 >= len(argv):
                raise ValueError("--demo requires clear-win or no-difference")
            values["demo"] = argv[index + 1]
            index += 2
            continue
        if item in {"--input", "-i", "--runner", "--report"}:
            if index + 1 >= len(argv):
                raise ValueError(f"{item} requires a value")
            key = {"-i": "input"}.get(item, item.lstrip("-"))
            values[key] = argv[index + 1]
            index += 2
            continue
        raise ValueError(f"unknown argument: {item}")
    return values


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float) and value >= 0:
        return float(value)
    return None


def _validate_verdict(verdict: Any) -> dict[str, Any]:
    if not isinstance(verdict, dict):
        raise ValueError("oracle_verdict must be an object")
    if verdict.get("choice") is None and verdict.get("choice_index") is None:
        raise ValueError("oracle_verdict needs choice or choice_index")
    return verdict


def _normalize_record(value: dict[str, Any], line_number: int) -> dict[str, Any]:
    schema = value.get("schema")
    if schema not in {ORACLE_SCHEMA, "eljev.decision/1"}:
        raise ValueError(
            f"line {line_number}: expected {ORACLE_SCHEMA} or eljev.decision/1"
        )

    task = value.get("task")
    if not isinstance(task, dict):
        task = {
            key: value[key]
            for key in ("path", "state", "question", "criterion", "candidates", "top_n", "tier")
            if key in value
        }
    if not task:
        raise ValueError(
            f"line {line_number}: task payload is required; include criterion/candidates "
            "or state/question"
        )

    expected_choice = value.get("expected_choice")
    if expected_choice is None:
        expected_choice = value.get("correct_choice")
    verdict = value.get("oracle_verdict")
    if verdict is None:
        verdict = {
            "schema": "eljev.decision/1",
            "choice": expected_choice,
            "choice_index": value.get("expected_choice_index"),
            "status": "selected",
            "exit_code": 0,
        }
    verdict = _validate_verdict(verdict)
    if expected_choice is None:
        expected_choice = verdict.get("choice")

    task_id = value.get("task_id") or value.get("decision_id")
    if not isinstance(task_id, str) or not task_id:
        raise ValueError(f"line {line_number}: task_id or decision_id is required")

    return {
        "schema": ORACLE_SCHEMA,
        "task_id": task_id,
        "decision_id": value.get("decision_id"),
        "task": task,
        "expected_choice": expected_choice,
        "oracle_verdict": verdict,
        "trigger": value.get("trigger", "userPromptTransformed"),
        "metadata": value.get("metadata", {}),
    }


def _load_records(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"line {line_number}: invalid JSON: {error}") from error
            if not isinstance(value, dict):
                raise ValueError(f"line {line_number}: record must be a JSON object")
            records.append(_normalize_record(value, line_number))
    if not records:
        raise ValueError("input JSONL contains no records")
    return records


class RunnerSession:
    """Persistent line-oriented adapter process to avoid per-decision launches."""

    def __init__(self, runner: Path, arm: str, timeout_s: float = 30.0) -> None:
        self.arm = arm
        self.timeout_s = timeout_s
        environment = os.environ.copy()
        environment["ELJEV_ORACLE_ARM"] = arm
        environment["ELJEV_ORACLE_PROTOCOL"] = REQUEST_SCHEMA
        self.process = subprocess.Popen(
            [sys.executable, str(runner)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=environment,
        )
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        self.responses: Queue[str | BaseException] = Queue()
        self.reader = threading.Thread(
            target=self._read_output,
            name=f"eljev-oracle-{arm}",
            daemon=True,
        )
        self.reader.start()

    def _read_output(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                self.responses.put(line)
        except BaseException as error:
            self.responses.put(error)

    def request(self, record: dict[str, Any]) -> tuple[dict[str, Any], float]:
        if self.process.poll() is not None:
            raise RuntimeError(f"{self.arm} runner exited with code {self.process.returncode}")
        assert self.process.stdin is not None
        request = {
            "schema": REQUEST_SCHEMA,
            "arm": self.arm,
            "trigger": record["trigger"],
            "task_id": record["task_id"],
            "task": record["task"],
            "oracle_verdict": (
                record["oracle_verdict"] if self.arm == "oracle" else None
            ),
        }
        started = time.perf_counter()
        self.process.stdin.write(
            json.dumps(request, ensure_ascii=True, separators=(",", ":")) + "\n"
        )
        self.process.stdin.flush()
        try:
            response = self.responses.get(timeout=self.timeout_s)
        except Empty as error:
            raise TimeoutError(f"{self.arm} runner exceeded {self.timeout_s:g}s") from error
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        if isinstance(response, BaseException):
            raise response
        try:
            value = json.loads(response)
        except json.JSONDecodeError as error:
            raise ValueError(f"{self.arm} runner emitted invalid JSON: {response!r}") from error
        if not isinstance(value, dict) or value.get("schema") != RESULT_SCHEMA:
            raise ValueError(f"{self.arm} runner emitted the wrong result schema")
        return value, elapsed_ms

    def close(self) -> None:
        if self.process.poll() is None:
            if self.process.stdin is not None:
                self.process.stdin.close()
            try:
                self.process.terminate()
                self.process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                if self.process.poll() is None:
                    self.process.kill()


def _result_metrics(result: dict[str, Any], measured_ms: float) -> dict[str, Any]:
    correct = result.get("outcome_correct")
    if not isinstance(correct, bool):
        correct = result.get("correct")
    if not isinstance(correct, bool):
        raise ValueError("runner result needs boolean outcome_correct")
    wall_ms = _number(result.get("task_time_ms"))
    if wall_ms is None:
        wall_ms = _number(result.get("wall_ms"))
    if wall_ms is None:
        wall_ms = measured_ms
    turns = _number(result.get("turns"))
    tokens = _number(result.get("tokens"))
    if tokens is None:
        input_tokens = _number(result.get("input_tokens"))
        output_tokens = _number(result.get("output_tokens"))
        if input_tokens is not None and output_tokens is not None:
            tokens = input_tokens + output_tokens
    return {
        "wall_ms": wall_ms,
        "turns": turns,
        "tokens": tokens,
        "outcome_correct": correct,
    }


def _run_external(
    records: list[dict[str, Any]], runner_path: Path
) -> list[dict[str, Any]]:
    baseline = RunnerSession(runner_path, "baseline")
    oracle = RunnerSession(runner_path, "oracle")
    pairs: list[dict[str, Any]] = []
    try:
        for record in records:
            baseline_result, baseline_elapsed = baseline.request(record)
            oracle_result, oracle_elapsed = oracle.request(record)
            pairs.append(
                {
                    "task_id": record["task_id"],
                    "baseline": _result_metrics(baseline_result, baseline_elapsed),
                    "oracle": _result_metrics(oracle_result, oracle_elapsed),
                }
            )
    finally:
        baseline.close()
        oracle.close()
    return pairs


def _demo_records() -> list[dict[str, Any]]:
    return [
        {
            "schema": ORACLE_SCHEMA,
            "task_id": f"demo-{index:02d}",
            "task": {
                "criterion": "most urgent",
                "candidates": [
                    {"id": "c1", "text": "routine item"},
                    {"id": "c2", "text": "urgent item"},
                ],
            },
            "expected_choice": "c2",
            "oracle_verdict": {
                "schema": "eljev.decision/1",
                "choice": "c2",
                "choice_index": 1,
                "status": "selected",
                "exit_code": 0,
            },
            "trigger": "userPromptTransformed",
        }
        for index in range(12)
    ]


def _run_demo(records: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    pairs: list[dict[str, Any]] = []
    for record in records:
        if kind == "clear-win":
            baseline = {"wall_ms": 120.0, "turns": 3.0, "tokens": 600.0}
            oracle = {"wall_ms": 20.0, "turns": 1.0, "tokens": 180.0}
        else:
            baseline = {"wall_ms": 80.0, "turns": 2.0, "tokens": 400.0}
            oracle = {"wall_ms": 80.0, "turns": 2.0, "tokens": 400.0}
        pairs.append(
            {
                "task_id": record["task_id"],
                "baseline": {**baseline, "outcome_correct": True},
                "oracle": {**oracle, "outcome_correct": True},
            }
        )
    return pairs


def _metric_summary(
    pairs: list[dict[str, Any]], metric: str
) -> dict[str, float | int | None]:
    baseline_values = [
        pair["baseline"][metric] for pair in pairs if pair["baseline"][metric] is not None
    ]
    oracle_values = [
        pair["oracle"][metric] for pair in pairs if pair["oracle"][metric] is not None
    ]
    if not baseline_values or not oracle_values:
        return {
            "n": 0,
            "baseline_mean": None,
            "oracle_mean": None,
            "baseline_median": None,
            "oracle_median": None,
            "relative_reduction": None,
            "median_paired_reduction": None,
        }
    paired = [
        pair["baseline"][metric] - pair["oracle"][metric]
        for pair in pairs
        if pair["baseline"][metric] is not None and pair["oracle"][metric] is not None
    ]
    baseline_mean = statistics.mean(baseline_values)
    oracle_mean = statistics.mean(oracle_values)
    return {
        "n": len(paired),
        "baseline_mean": baseline_mean,
        "oracle_mean": oracle_mean,
        "baseline_median": statistics.median(baseline_values),
        "oracle_median": statistics.median(oracle_values),
        "relative_reduction": (
            (baseline_mean - oracle_mean) / baseline_mean if baseline_mean else None
        ),
        "median_paired_reduction": statistics.median(paired),
    }


def _analyze(pairs: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(pairs)
    baseline_correct = sum(pair["baseline"]["outcome_correct"] for pair in pairs)
    oracle_correct = sum(pair["oracle"]["outcome_correct"] for pair in pairs)
    baseline_rate = baseline_correct / n if n else 0.0
    oracle_rate = oracle_correct / n if n else 0.0
    summaries = {
        metric: _metric_summary(pairs, metric)
        for metric in ("wall_ms", "turns", "tokens")
    }
    wall = summaries["wall_ms"]
    turns = summaries["turns"]
    tokens = summaries["tokens"]
    wall_win = bool(
        wall["relative_reduction"] is not None
        and wall["relative_reduction"] >= MATERIAL_RELATIVE_REDUCTION
        and (wall["median_paired_reduction"] or 0) > 0
    )
    turns_win = bool(
        turns["median_paired_reduction"] is not None
        and turns["median_paired_reduction"] >= MATERIAL_TURN_REDUCTION
    )
    tokens_win = bool(
        tokens["relative_reduction"] is not None
        and tokens["relative_reduction"] >= MATERIAL_RELATIVE_REDUCTION
        and (tokens["median_paired_reduction"] or 0) > 0
    )
    reasons: list[str] = []
    if n < MIN_PAIRS:
        reasons.append(f"sample has {n} pairs; at least {MIN_PAIRS} are required")
    if oracle_correct < baseline_correct:
        reasons.append("oracle outcome correctness regressed")
    if not (wall_win or turns_win or tokens_win):
        reasons.append("no material wall-time, turn, or token improvement")
    verdict = "PROCEED" if not reasons else "STOP"
    return {
        "pairs": n,
        "baseline_correct": baseline_correct,
        "oracle_correct": oracle_correct,
        "baseline_correctness": baseline_rate,
        "oracle_correctness": oracle_rate,
        "paired_results": pairs,
        "metrics": summaries,
        "material_wins": {
            "wall_ms": wall_win,
            "turns": turns_win,
            "tokens": tokens_win,
        },
        "verdict": verdict,
        "reasons": reasons,
    }


def _print_report(report: dict[str, Any]) -> None:
    print("S1 ORACLE CEILING - KILL GATE")
    print(f"Paired tasks: {report['pairs']}")
    print(
        "Outcome correctness: "
        f"baseline {report['baseline_correctness']:.1%}; "
        f"oracle {report['oracle_correctness']:.1%}"
    )
    for metric in ("wall_ms", "turns", "tokens"):
        summary = report["metrics"][metric]
        if summary["n"] == 0:
            print(f"{metric}: unavailable")
            continue
        print(
            f"{metric}: baseline median {summary['baseline_median']:.2f}; "
            f"oracle median {summary['oracle_median']:.2f}; "
            f"mean reduction {summary['relative_reduction']:.1%}"
        )
    if report["reasons"]:
        print("STOP - DO NOT BUILD THE ENGINE YET")
        for reason in report["reasons"]:
            print(f"  - {reason}")
    else:
        print("PROCEED - the instant known-correct trigger clears the S1 gate")


def main(argv: list[str]) -> int:
    try:
        options = _parse_args(argv)
        demo = options["demo"]
        if demo is not None:
            if demo not in {"clear-win", "no-difference"}:
                raise ValueError("--demo must be clear-win or no-difference")
            records = _demo_records()
            pairs = _run_demo(records, demo)
        else:
            if options["input"] is None or options["runner"] is None:
                raise ValueError("--input and --runner are required unless --demo is used")
            records = _load_records(Path(options["input"]))
            pairs = _run_external(records, Path(options["runner"]))
        report = _analyze(pairs)
        _print_report(report)
        if options["report"] is not None:
            report_path = Path(options["report"])
            report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(
                json.dumps(report, indent=2, ensure_ascii=True) + "\n",
                encoding="utf-8",
            )
        return 0 if report["verdict"] == "PROCEED" else 2
    except (OSError, ValueError, RuntimeError, TimeoutError) as error:
        print(f"S1 harness error: {error}", file=sys.stderr)
        print(_usage(), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
