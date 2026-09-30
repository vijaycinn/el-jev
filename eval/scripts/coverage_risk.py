"""Evaluate the two-dimensional coverage/selective-risk surface on held-out data."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from _common import (
    HarnessError,
    choose_dual_operating_point,
    is_exact_top_tie,
    label_is_correct,
    load_corpus,
    load_labels,
    load_split,
    model_ranking,
    primary_labels,
    read_json,
    sha256_file,
    tier_name,
    write_json,
)
from calibrate import calibrated_gate_features


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--split-manifest", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--risk-budget", type=float)
    parser.add_argument("--confidence", type=float)
    parser.add_argument("--minimum-coverage", type=float, default=0.10)
    return parser.parse_args()


def tier_breakdown(
    rows: list[tuple[dict[str, Any], dict[str, Any]]],
    calibration: dict[str, Any],
    threshold: float,
    margin_threshold: float,
    confidence: float,
    risk_budget: float,
) -> dict[str, Any]:
    from _common import accuracy_lower_bound, risk_upper_bound

    grouped: dict[str, dict[str, Any]] = {}
    for record, label in rows:
        tier = tier_name(record)
        grouped.setdefault(tier, {"total": 0, "values": []})
        grouped[tier]["total"] += 1
        features = calibrated_gate_features(record, calibration)
        if features is None:
            continue
        grouped[tier]["values"].append(
            (features["top1_probability"], features["margin"], label_is_correct(record, label))
        )
    result: dict[str, Any] = {}
    for tier, group in sorted(grouped.items()):
        values = group["values"]
        acted = [
            correct
            for probability, margin, correct in values
            if probability >= threshold and margin >= margin_threshold
        ]
        errors = sum(not correct for correct in acted)
        point_risk = errors / len(acted) if acted else None
        bound = (
            risk_upper_bound(errors, len(acted), confidence)
            if acted and point_risk is not None and point_risk <= risk_budget
            else None
        )
        result[tier] = {
            "total_decisions": group["total"],
            "total_scoreable_non_tied": len(values),
            "acted_count": len(acted),
            "coverage": len(acted) / group["total"] if group["total"] else 0.0,
            "errors": errors,
            "selective_risk": point_risk,
            "accuracy_lower_bound": 1.0 - bound if bound is not None else None,
            "risk_upper_bound": bound,
        }
    return result


def main() -> int:
    args = parse_args()
    try:
        calibration = read_json(args.calibration)
        risk_budget = (
            float(args.risk_budget)
            if args.risk_budget is not None
            else float(calibration.get("risk_budget", 0.02))
        )
        confidence = (
            float(args.confidence)
            if args.confidence is not None
            else float(calibration.get("confidence", 0.95))
        )
        if not 0 < risk_budget < 1:
            raise HarnessError("--risk-budget must be between 0 and 1")
        if not 0 < confidence < 1:
            raise HarnessError("--confidence must be between 0 and 1")
        if not 0 <= args.minimum_coverage <= 1:
            raise HarnessError("--minimum-coverage must be between 0 and 1")
        corpus = load_corpus(args.corpus)
        labels = load_labels(args.labels, corpus)
        primary = primary_labels(labels)
        split = load_split(args.split_manifest)
        if calibration.get("corpus_hash") != sha256_file(args.corpus):
            raise HarnessError("calibration corpus hash does not match corpus")
        if calibration.get("labels_hash") != sha256_file(args.labels):
            raise HarnessError("calibration labels hash does not match labels")
        if calibration.get("split_hash") != sha256_file(args.split_manifest):
            raise HarnessError("calibration split hash does not match split manifest")
        heldout_ids = set(split["heldout_ids"])
        if not heldout_ids:
            raise HarnessError("held-out split is empty")
        by_id = {record["record_id"]: record for record in corpus}
        missing = heldout_ids - set(by_id) - set(primary)
        if missing:
            raise HarnessError(
                f"held-out records missing from corpus or labels: {', '.join(sorted(missing))}"
            )
        rows = [
            (dict(by_id[record_id], _split="heldout"), dict(primary[record_id]))
            for record_id in sorted(heldout_ids)
        ]
        pairs: list[tuple[float, float, bool]] = []
        tied = 0
        single_candidate = 0
        unscoreable = 0
        for record, label in rows:
            ranking = model_ranking(record)
            if len(ranking) < 2:
                single_candidate += 1
                continue
            if is_exact_top_tie(ranking):
                tied += 1
                continue
            features = calibrated_gate_features(record, calibration)
            if features is None:
                unscoreable += 1
                continue
            pairs.append(
                (
                    features["top1_probability"],
                    features["margin"],
                    label_is_correct(record, label),
                )
            )
        chosen, surface, evaluated_points = choose_dual_operating_point(
            pairs, len(rows), risk_budget, confidence
        )
        warning = chosen["coverage"] < args.minimum_coverage
        output = {
            "schema": "eljev.eval.coverage-risk/1",
            "corpus_hash": sha256_file(args.corpus),
            "labels_hash": sha256_file(args.labels),
            "split_hash": sha256_file(args.split_manifest),
            "split_manifest": str(args.split_manifest),
            "calibration": str(args.calibration),
            "risk_budget": risk_budget,
            "confidence": confidence,
            "minimum_coverage": args.minimum_coverage,
            "heldout": {
                "total_decisions": len(rows),
                "scoreable_non_tied": len(pairs),
                "exact_top_ties": tied,
                "single_candidate": single_candidate,
                "unscoreable": unscoreable,
            },
            "operating_point": chosen,
            "surface_points_evaluated": evaluated_points,
            "coverage_risk_surface": surface,
            "per_tier": tier_breakdown(
                rows,
                calibration,
                chosen["threshold"],
                chosen["margin_threshold"],
                confidence,
                risk_budget,
            ),
            "coverage_warning": (
                "UNUSUABLY LOW COVERAGE: no defensible (threshold, margin_threshold) "
                f"pair reaches the configured {args.minimum_coverage:.1%} minimum at the "
                "requested risk bound."
                if warning
                else None
            ),
        }
        write_json(args.output, output)
        if warning:
            print(output["coverage_warning"], file=sys.stderr)
        print(
            f"held-out coverage={chosen['coverage']:.2%} "
            f"risk_upper_bound={chosen['risk_upper_bound']!s} "
            f"threshold={chosen['threshold']:.8g} "
            f"margin_threshold={chosen['margin_threshold']:.8g}"
        )
        print(f"wrote {args.output}")
        return 0
    except (HarnessError, KeyError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
