"""Compute exact one-sided sample-size requirements for selective-risk claims."""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

from _common import HarnessError, load_corpus, load_labels, primary_labels


def required_error_free_trials(risk_budget: float, confidence: float) -> int:
    return max(1, math.ceil(math.log(1.0 - confidence) / math.log(1.0 - risk_budget)))


def upper_bound(errors: int, trials: int, confidence: float) -> float:
    from _common import risk_upper_bound

    return risk_upper_bound(errors, trials, confidence)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--risk-budget", "--risk", type=float, default=0.02)
    parser.add_argument("--confidence", type=float, default=0.95)
    parser.add_argument("--development-fraction", type=float, default=0.70)
    parser.add_argument(
        "--expected-coverage",
        type=float,
        default=1.0,
        help="Expected acted-on fraction; lower coverage requires more labelled rows",
    )
    parser.add_argument("--available-labels", type=int)
    parser.add_argument("--available-heldout", type=int)
    parser.add_argument("--heldout-errors", type=int, default=0)
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--labels", type=Path)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if not 0 < args.risk_budget < 1:
            raise HarnessError("--risk-budget must be between 0 and 1")
        if not 0 < args.confidence < 1:
            raise HarnessError("--confidence must be between 0 and 1")
        if not 0 < args.development_fraction < 1:
            raise HarnessError("--development-fraction must be between 0 and 1")
        if not 0 < args.expected_coverage <= 1:
            raise HarnessError("--expected-coverage must be in (0, 1]")
        if args.heldout_errors < 0:
            raise HarnessError("--heldout-errors cannot be negative")
        error_free_acted = required_error_free_trials(args.risk_budget, args.confidence)
        required_heldout = math.ceil(error_free_acted / args.expected_coverage)
        required_total = math.ceil(required_heldout / (1.0 - args.development_fraction))
        print(f"Selective-risk claim: risk <= {args.risk_budget:.2%}")
        print(f"One-sided confidence: {args.confidence:.2%}")
        print(
            f"With zero acted-on errors, collect at least {error_free_acted} acted-on "
            "held-out decisions."
        )
        print(
            f"At expected coverage {args.expected_coverage:.2%}, reserve at least "
            f"{required_heldout} held-out labels."
        )
        print(
            f"With development fraction {args.development_fraction:.2%}, collect at least "
            f"{required_total} total labels before fitting."
        )
        print("\nError sensitivity (held-out acted-on trials):")
        for errors in range(0, 4):
            trials = error_free_acted
            while upper_bound(errors, trials, args.confidence) > args.risk_budget:
                trials += 1
            print(
                f"  {errors} error(s): {trials} acted-on trials "
                f"(upper risk {upper_bound(errors, trials, args.confidence):.3%})"
            )
        if args.corpus and args.labels:
            corpus = load_corpus(args.corpus)
            labels = load_labels(args.labels, corpus)
            available = len(primary_labels(labels))
            print(f"\nAvailable primary-labelled records: {available}")
            if available < required_total:
                print(
                    "WARNING: corpus is too small for the requested claim; "
                    f"need at least {required_total} labels before fitting.",
                    file=sys.stderr,
                )
        if args.available_labels is not None and args.available_labels < required_total:
            print(
                "WARNING: requested claim is not supportable by the available label count.",
                file=sys.stderr,
            )
        if args.available_heldout is not None:
            if args.available_heldout < required_heldout:
                print(
                    "WARNING: held-out corpus is too small for the zero-error claim.",
                    file=sys.stderr,
                )
            if args.heldout_errors > 0:
                bound = upper_bound(args.heldout_errors, args.available_heldout, args.confidence)
                if bound > args.risk_budget:
                    print(
                        f"WARNING: observed errors imply an upper risk bound of {bound:.3%}; "
                        "the requested claim is not supportable.",
                        file=sys.stderr,
                    )
        return 0
    except HarnessError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
