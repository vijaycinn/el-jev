"""LLM sub-agent vs el-jev: decision speed and accuracy on the same labelled intent prompts.

Each LLM baseline answers the hook's five-way intent question in a fresh non-interactive
Copilot CLI session (`copilot -p`). Its decision time is the summed
`model.call_finished.dispatchDurationMs`, so CLI and MCP start-up are excluded. el-jev is
measured through the running daemon (decision only) and through the hook process
(Python start plus decision).

Arms:
  lean  MCP servers, built-in MCPs and custom instructions disabled: the LLM best case.
  full  the Copilot context of --cwd (MCP servers, skills, instructions): what a real
        session loads before it can answer.

Requires a running daemon and the `copilot` CLI on PATH. Every LLM call is a billed request.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "hooks"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import eljev_pre_turn as hook  # noqa: E402
from eljev.client import EljevClient  # noqa: E402
from functional_eval import INTENT_QUESTION, LABELLED_INTENTS  # noqa: E402

COPILOT_HOME = Path.home() / ".copilot"
DEFAULT_REPORTS = REPO / "eval" / "reports"
DEFAULT_AGENTS = ("general-purpose", "task")
LABELS = list(hook.INTENT_CRITERIA)
EXPECTED_STATUSES = {"needs_review", "selected", "abstain_tie"}


def _llm_prompt(request: str) -> str:
    labels = "; ".join(f"{label} ({description})" for label, description in hook.INTENT_CRITERIA.items())
    return (
        "Classify the developer request into exactly one intent label. Reply with the label only. "
        f"Do not use tools. Labels: {labels}. Request: {request}"
    )


def _epoch_ms(value: str) -> float:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000.0


def _parse_label(text: str) -> str | None:
    lowered = (text or "").strip().lower()
    hits = [label for label in LABELS if label in lowered]
    return hits[0] if len(hits) == 1 else None


def _percentile(values: list[float], quantile: float) -> float | None:
    ordered = sorted(value for value in values if value is not None)
    if not ordered:
        return None
    position = (len(ordered) - 1) * quantile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 1)


def _default_baselines() -> list[dict[str, str]]:
    try:
        settings = json.loads((COPILOT_HOME / "settings.json").read_text(encoding="utf-8"))
        agents = settings.get("subagents", {}).get("agents", {})
    except (OSError, ValueError, AttributeError):
        agents = {}
    baselines = []
    for name in DEFAULT_AGENTS:
        entry = agents.get(name) or {}
        if entry.get("model"):
            baselines.append({"agent": name, "model": entry["model"], "effort": entry.get("effortLevel", "medium")})
    return baselines


def _parse_baseline(value: str) -> dict[str, str]:
    """AGENT=MODEL@EFFORT, e.g. task=gpt-5.6-luna@max."""
    try:
        agent, rest = value.split("=", 1)
        model, effort = rest.split("@", 1) if "@" in rest else (rest, "medium")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("baseline must be AGENT=MODEL@EFFORT") from exc
    return {"agent": agent.strip(), "model": model.strip(), "effort": effort.strip()}


def _configured_mcp_servers() -> list[str]:
    try:
        config = json.loads((COPILOT_HOME / "mcp-config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    servers = config.get("mcpServers") if isinstance(config, dict) else None
    return sorted(servers) if isinstance(servers, dict) else []


def run_llm(
    copilot: str, baseline: dict[str, str], label: str, request: str, *, arm: str, workdir: str, disabled: list[str]
) -> dict[str, Any]:
    command = [
        copilot, "-p", _llm_prompt(request), "--model", baseline["model"],
        "--reasoning-effort", baseline["effort"], "--output-format", "json", "--no-color",
    ]
    if arm == "lean":
        command += ["--no-custom-instructions", "--disable-builtin-mcps"]
        for server in disabled:
            command += ["--disable-mcp-server", server]
    started = time.perf_counter()
    try:
        process = subprocess.run(
            command, cwd=workdir, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=300, env=dict(os.environ, EL_JEV="OFF"),
        )
        stdout = process.stdout
    except subprocess.TimeoutExpired:
        stdout = ""
    wall_ms = (time.perf_counter() - started) * 1000.0

    dispatch_ms, calls, answer = 0.0, 0, ""
    call_start = first_delta = turn_start = final_message = None
    mcp_connected: int | None = None
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind, data = event.get("type"), event.get("data") or {}
        if kind == "session.mcp_servers_loaded":
            mcp_connected = sum(1 for server in data.get("servers", []) if server.get("status") == "connected")
        elif kind == "model.call_start" and call_start is None:
            call_start = _epoch_ms(event["timestamp"])
        elif kind == "model.call_finished":
            dispatch_ms += float(data.get("dispatchDurationMs") or 0.0)
            calls += 1
        elif kind == "assistant.turn_start" and turn_start is None:
            turn_start = _epoch_ms(event["timestamp"])
        elif kind == "assistant.message_delta" and first_delta is None:
            first_delta = _epoch_ms(event["timestamp"])
        elif kind == "assistant.message":
            answer = data.get("content") or answer
            final_message = _epoch_ms(event["timestamp"])

    choice = _parse_label(answer)
    return {
        "system": baseline["agent"], "model": baseline["model"], "effort": baseline["effort"],
        "label": label, "prompt": request, "answer": answer.strip()[:80], "choice": choice,
        "correct": choice == label, "ok": calls > 0 and bool(answer),
        "decision_ms": round(dispatch_ms, 1) if calls else None,
        "model_calls": calls,
        "mcp_servers_connected": mcp_connected,
        "time_to_answer_ms": round(first_delta - call_start, 1) if first_delta and call_start else None,
        "turn_ms": round(final_message - turn_start, 1) if final_message and turn_start else None,
        "wall_ms": round(wall_ms, 1),
    }


def run_eljev(cases: list[tuple[str, str]]) -> list[dict[str, Any]]:
    client = EljevClient(timeout_s=10)
    client.decide("warm up the connection before measuring", INTENT_QUESTION)
    rows = []
    for label, request in cases:
        started = time.perf_counter()
        record = client.decide(request, INTENT_QUESTION)
        decision_ms = (time.perf_counter() - started) * 1000.0
        started = time.perf_counter()
        subprocess.run(
            [sys.executable, str(REPO / "hooks" / "eljev_pre_turn.py")],
            input=json.dumps({"prompt": request, "sessionId": "llm-vs-eljev"}),
            capture_output=True, text=True, timeout=15, env=dict(os.environ, EL_JEV="ON"),
        )
        hook_wall_ms = (time.perf_counter() - started) * 1000.0
        rows.append({
            "system": "el-jev", "model": record.get("engine"), "effort": "-",
            "label": label, "prompt": request, "choice": record.get("choice"),
            "correct": record.get("choice") == label, "ok": record.get("status") in EXPECTED_STATUSES,
            "decision_ms": round(decision_ms, 1), "p_top": record.get("calibrated_probability"),
            "hook_wall_ms": round(hook_wall_ms, 1),
        })
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [row for row in rows if row["ok"]]
    decisions = [row["decision_ms"] for row in ok]
    summary: dict[str, Any] = {
        "n": len(rows),
        "ok": len(ok),
        "accuracy": f"{sum(row['correct'] for row in rows)}/{len(rows)}",
        "decision_p50": _percentile(decisions, 0.5),
        "decision_p95": _percentile(decisions, 0.95),
        "decision_min": min(decisions) if decisions else None,
        "decision_max": max(decisions) if decisions else None,
        "decision_mean": round(statistics.mean(decisions), 1) if decisions else None,
    }
    if rows and "wall_ms" in rows[0]:
        summary.update({
            "time_to_answer_p50": _percentile([row["time_to_answer_ms"] for row in ok], 0.5),
            "turn_p50": _percentile([row["turn_ms"] for row in ok], 0.5),
            "cli_wall_p50": _percentile([row["wall_ms"] for row in rows], 0.5),
            "multi_call_turns": sum(1 for row in ok if row["model_calls"] > 1),
            "mcp_servers_connected_max": max(
                (row["mcp_servers_connected"] for row in rows if row["mcp_servers_connected"] is not None), default=None
            ),
        })
    if rows and "hook_wall_ms" in rows[0]:
        summary["hook_wall_p50"] = _percentile([row["hook_wall_ms"] for row in rows], 0.5)
        summary["hook_wall_p95"] = _percentile([row["hook_wall_ms"] for row in rows], 0.95)
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arm", choices=("lean", "full"), default="lean")
    parser.add_argument("--per-intent", type=int, default=6, help="Prompts per intent, 1-6 (default 6 = 30 prompts)")
    parser.add_argument("--workers", type=int, default=3, help="Concurrent Copilot sessions per baseline")
    parser.add_argument(
        "--baseline", action="append", type=_parse_baseline, metavar="AGENT=MODEL@EFFORT",
        help="LLM baseline; repeatable. Default: general-purpose and task from ~/.copilot/settings.json",
    )
    parser.add_argument("--cwd", type=Path, default=Path.cwd(), help="Workspace whose Copilot context the full arm loads")
    parser.add_argument(
        "--disable-mcp", action="append", default=[], metavar="NAME",
        help="Extra MCP server to disable in the lean arm (plugin servers); repeatable",
    )
    parser.add_argument("--out", type=Path, help="Report path (default: eval/reports/llm-vs-eljev-<arm>-<utc>.json)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    copilot = shutil.which("copilot")
    if copilot is None:
        print("copilot CLI not found on PATH", file=sys.stderr)
        return 2
    if EljevClient().health().get("status") != "ok":
        print("Daemon is not healthy. Run: python -m eljev daemon start", file=sys.stderr)
        return 2
    baselines = args.baseline or _default_baselines()
    if not baselines:
        print("No baselines: pass --baseline AGENT=MODEL@EFFORT", file=sys.stderr)
        return 2
    per_intent = max(1, min(args.per_intent, 6))
    cases = [(label, prompt) for label, prompts in LABELLED_INTENTS.items() for prompt in prompts[:per_intent]]
    disabled = sorted(set(_configured_mcp_servers()) | set(args.disable_mcp)) if args.arm == "lean" else []

    print(f"el-jev: {len(cases)} prompts", flush=True)
    results: dict[str, list[dict[str, Any]]] = {"el-jev": run_eljev(cases)}
    print(f"  {json.dumps(summarize(results['el-jev']))}", flush=True)

    with tempfile.TemporaryDirectory(prefix="eljev-llm-bench-") as scratch:
        workdir = scratch if args.arm == "lean" else str(args.cwd)
        for baseline in baselines:
            print(f"{baseline['agent']} ({baseline['model']} @ {baseline['effort']}): {len(cases)} prompts", flush=True)
            with cf.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
                futures = [
                    pool.submit(run_llm, copilot, baseline, label, request, arm=args.arm, workdir=workdir, disabled=disabled)
                    for label, request in cases
                ]
                rows = []
                for future in cf.as_completed(futures):
                    row = future.result()
                    rows.append(row)
                    print(f"  [{len(rows):2}/{len(cases)}] {row['decision_ms']} ms  {row['choice']}  "
                          f"({'ok' if row['correct'] else 'MISS'})", flush=True)
            results[baseline["agent"]] = rows
            print(f"  {json.dumps(summarize(rows))}", flush=True)

    report = {
        "schema": "eljev.eval.llm_vs_eljev/1",
        "run_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "arm": args.arm,
        "conditions": {
            "prompts": len(cases),
            "workers_per_baseline": args.workers,
            "baselines": baselines,
            "lean_disabled_mcp_servers": disabled,
            "llm_decision_ms": "sum of model.call_finished.dispatchDurationMs (excludes CLI and MCP start-up)",
            "eljev_decision_ms": "daemon /v1/decide round trip, warm",
            "eljev_hook_wall_ms": "hook process wall time: Python start plus decision",
        },
        "summary": {system: summarize(rows) for system, rows in results.items()},
        "rows": results,
    }
    out = args.out or DEFAULT_REPORTS / f"llm-vs-eljev-{args.arm}-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"\n{'system':18} {'accuracy':>9} {'p50 ms':>9} {'p95 ms':>9}")
    for system, summary in report["summary"].items():
        print(f"{system:18} {summary['accuracy']:>9} {summary['decision_p50']!s:>9} {summary['decision_p95']!s:>9}")
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
