"""Create a deterministic, immutable development/held-out split manifest."""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

from _common import (
    HarnessError,
    load_corpus,
    load_labels,
    primary_labels,
    sha256_file,
    utc_now,
    write_json,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--heldout-fraction", type=float, default=0.25)
    parser.add_argument("--seed", default="eljev-eval-v1")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if not 0 < args.heldout_fraction < 1:
            raise HarnessError("--heldout-fraction must be between 0 and 1")
        corpus = load_corpus(args.corpus)
        labels = load_labels(args.labels, corpus)
        primary = primary_labels(labels)
        corpus_ids = {record["record_id"] for record in corpus}
        unknown = sorted(set(primary) - corpus_ids)
        if unknown:
            raise HarnessError(f"labels contain records missing from corpus: {', '.join(unknown)}")
        ids = sorted(primary)
        if len(ids) < 2:
            raise HarnessError("at least two primary-labelled records are required")
        ranked = sorted(
            ids,
            key=lambda record_id: hashlib.sha256(f"{args.seed}\0{record_id}".encode()).hexdigest(),
        )
        heldout_count = max(1, min(len(ids) - 1, round(len(ids) * args.heldout_fraction)))
        heldout = sorted(ranked[:heldout_count])
        development = sorted(ranked[heldout_count:])
        manifest = {
            "schema": "eljev.eval.split/1",
            "created_at": utc_now(),
            "seed": args.seed,
            "heldout_fraction": args.heldout_fraction,
            "corpus_hash": sha256_file(args.corpus),
            "labels_hash": sha256_file(args.labels),
            "labelled_count": len(ids),
            "development_ids": development,
            "heldout_ids": heldout,
            "split_sizes": {
                "development": len(development),
                "heldout": len(heldout),
                "labelled": len(ids),
            },
        }
        write_json(args.output, manifest)
        print(
            f"wrote split: development={len(development)} heldout={len(heldout)} "
            f"manifest={args.output}"
        )
        return 0
    except HarnessError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
