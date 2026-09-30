"""Capture replayable, redacted decision records outside the repository."""

from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path
from typing import Any

from _common import (
    DEFAULT_CORPUS,
    HarnessError,
    candidate_ids,
    ensure_parent,
    read_jsonl,
    redact_candidate,
    reject_repository_corpus_path,
    utc_now,
    write_jsonl,
)


def capture_record(raw: dict[str, Any]) -> dict[str, Any]:
    candidates = raw.get("candidate_payload", raw.get("candidates"))
    if not isinstance(candidates, list) or not candidates:
        raise HarnessError("capture input needs a non-empty candidates array")
    if len(candidates) > 250:
        raise HarnessError("capture input exceeds the 250-candidate cap")
    total_chars = 0
    for candidate in candidates:
        text = candidate.get("text") if isinstance(candidate, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise HarnessError("every candidate needs non-empty text")
        if len(text) > 2000:
            raise HarnessError(f"candidate {candidate.get('id', '<unknown>')} exceeds 2000 characters")
        total_chars += len(text)
    if total_chars > 100_000:
        raise HarnessError("capture input exceeds the 100,000-character total cap")
    redacted_candidates = [redact_candidate(candidate) for candidate in candidates]
    ids = candidate_ids({"candidate_payload": redacted_candidates})
    if len(ids) != len(set(ids)):
        raise HarnessError("candidate ids must be unique")
    lengths = [len(candidate["text"]) for candidate in redacted_candidates]
    elapsed = raw.get("task_wall_ms")
    if elapsed is None and isinstance(raw.get("elapsed_ms"), dict):
        elapsed = raw["elapsed_ms"].get("total")
    if elapsed is None:
        raise HarnessError("capture input needs task_wall_ms or elapsed_ms.total")
    record: dict[str, Any] = {
        "schema": "eljev.eval.corpus/1",
        "record_id": str(raw.get("record_id") or raw.get("decision_id") or uuid.uuid4()),
        "captured_at": str(raw.get("captured_at") or utc_now()),
        "criterion": raw.get("criterion"),
        "candidate_payload": redacted_candidates,
        "candidate_ids": ids,
        "n_candidates": len(redacted_candidates),
        "candidate_lengths": lengths,
        "baseline_choice": raw.get("baseline_choice", raw.get("choice")),
        "deterministic_trigger": raw.get(
            "deterministic_trigger",
            raw.get("trigger", {"name": "unknown", "fired": False}),
        ),
        "task_wall_ms": float(elapsed),
        "turn_count": raw.get("turn_count"),
        "token_counts": raw.get("token_counts"),
        "tier_path": raw.get("tier_path", []),
        "engine": raw.get("engine"),
        "engine_version": raw.get("engine_version"),
        "model_results": raw.get("model_results", raw.get("results")),
        "redaction_policy": {
            "name": "standard-v1",
            "applied_at_entry": True,
            "raw_text_persisted": False,
        },
        "provenance": raw.get("provenance", {}),
    }
    if isinstance(raw.get("elapsed_ms"), dict):
        record["elapsed_ms"] = raw["elapsed_ms"]
    if record["criterion"] is None:
        raise HarnessError("criterion is required")
    return record


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Raw JSONL capture input")
    parser.add_argument("--output", type=Path, default=DEFAULT_CORPUS, help="External JSONL corpus path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        reject_repository_corpus_path(args.output)
        rows = [capture_record(row) for row in read_jsonl(args.input)]
        if not rows:
            raise HarnessError("capture input is empty")
        ensure_parent(args.output)
        write_jsonl(args.output, rows)
        print(f"captured {len(rows)} replayable redacted records to {args.output}")
        return 0
    except HarnessError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
