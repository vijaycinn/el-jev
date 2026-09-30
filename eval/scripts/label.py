"""Blindly label replayable corpus records with set-valued human judgements."""

from __future__ import annotations

import argparse
import random
import sys
import uuid
from pathlib import Path
from typing import Any

from _common import (
    DEFAULT_LABELS,
    HarnessError,
    candidate_ids,
    load_corpus,
    load_labels,
    normalize_choices,
    read_json,
    reject_repository_corpus_path,
    utc_now,
    write_jsonl,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--labeler-id", default="anonymous")
    parser.add_argument("--seed", default="eljev-label-v1")
    parser.add_argument("--record-id", action="append", dest="record_ids")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--relabel-from", type=Path, help="Existing labels; selects records for retest")
    parser.add_argument("--relabel-count", type=int)
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument(
        "--answer-key",
        type=Path,
        help="JSON object mapping record_id to a list of acceptable ids; [] means none",
    )
    return parser.parse_args()


def select_records(args: argparse.Namespace, corpus: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    by_id = {record["record_id"]: record for record in corpus}
    if args.relabel_from:
        existing = load_labels(args.relabel_from, corpus)
        ids = sorted({label["record_id"] for label in existing if label["annotation_kind"] == "primary"})
        if args.relabel_count is not None:
            if args.relabel_count <= 0:
                raise HarnessError("--relabel-count must be positive")
            random.Random(args.seed).shuffle(ids)
            ids = ids[: args.relabel_count]
        selected = [by_id[record_id] for record_id in ids if record_id in by_id]
        return selected, "retest"
    ids = args.record_ids or list(by_id)
    unknown = sorted(set(ids) - set(by_id))
    if unknown:
        raise HarnessError(f"requested record ids missing from corpus: {', '.join(unknown)}")
    selected = [by_id[record_id] for record_id in ids]
    if args.limit is not None:
        if args.limit <= 0:
            raise HarnessError("--limit must be positive")
        selected = selected[: args.limit]
    return selected, "primary"


def answer_key(path: Path) -> dict[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise HarnessError("answer key must be a JSON object")
    return value


def prompt_for_label(record: dict[str, Any], rng: random.Random) -> list[str]:
    candidates = list(record["candidate_payload"])
    rng.shuffle(candidates)
    print(f"\nRecord {record['record_id']}")
    print(f"Criterion: {record['criterion']}")
    print("Candidates (randomized; scores and baseline are intentionally not shown):")
    for candidate in candidates:
        print(f"\n[{candidate['id']}]\n{candidate['text']}")
    print("\nEnter one or more acceptable ids separated by commas, or 'none'.")
    return normalize_choices(input("Acceptable: ").strip(), set(candidate_ids(record)))


def main() -> int:
    args = parse_args()
    try:
        if args.simulate and not args.answer_key:
            raise HarnessError("--simulate requires --answer-key")
        reject_repository_corpus_path(args.output)
        corpus = load_corpus(args.corpus)
        selected, annotation_kind = select_records(args, corpus)
        if not selected:
            raise HarnessError("no records selected")
        rng = random.Random(args.seed)
        rng.shuffle(selected)
        key = answer_key(args.answer_key) if args.answer_key else {}
        existing_labels: list[dict[str, Any]] = []
        if args.relabel_from:
            existing_labels = load_labels(args.relabel_from, corpus)
        labels: list[dict[str, Any]] = []
        for record in selected:
            if args.simulate:
                if record["record_id"] not in key:
                    raise HarnessError(f"answer key has no entry for {record['record_id']}")
                choices = normalize_choices(key[record["record_id"]], set(candidate_ids(record)))
            else:
                choices = prompt_for_label(record, rng)
            labels.append(
                {
                    "schema": "eljev.eval.label/1",
                    "annotation_id": str(uuid.uuid4()),
                    "record_id": record["record_id"],
                    "labeler_id": args.labeler_id,
                    "annotation_kind": annotation_kind,
                    "acceptable_choices": choices,
                    "none": not choices,
                    "labeled_at": utc_now(),
                }
            )
        write_jsonl(args.output, [*existing_labels, *labels])
        print(
            f"wrote {len(labels)} blinded {annotation_kind} labels "
            f"({len(existing_labels)} existing retained) to {args.output}; "
            "empty acceptable_choices means none"
        )
        return 0
    except (HarnessError, EOFError, KeyboardInterrupt) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
