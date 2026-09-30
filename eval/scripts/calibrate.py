"""Fit the binding pointwise temperature and dual gate on development data only."""

from __future__ import annotations

import argparse
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from _common import (
    HarnessError,
    SplitGuardError,
    choose_dual_operating_point,
    gate_features,
    label_is_correct,
    load_corpus,
    load_labels,
    load_split,
    model_ranking,
    pointwise_probability,
    primary_labels,
    sha256_file,
    write_json,
)


def fit_temperature(rows: Sequence[tuple[dict[str, Any], dict[str, Any]]], _top_k: int) -> float:
    """Fit T for p(s)=sigmoid(logit(clip(s))/T); top_k is metadata only."""
    if not rows:
        raise SplitGuardError("development split is empty; fitting on held-out data is prohibited")
    if any(record.get("_split") != "development" for record, _ in rows):
        raise SplitGuardError("temperature fitting accepts development records only")
    usable: list[tuple[float, bool]] = []
    for record, label in rows:
        ranking = model_ranking(record)
        if len(ranking) < 2 or ranking[0]["relevance_score"] == ranking[1]["relevance_score"]:
            continue
        usable.append((ranking[0]["relevance_score"], label_is_correct(record, label)))
    if not usable:
        raise HarnessError("development split has no non-tied model rankings")

    def objective(log_temperature: float) -> float:
        temperature = math.exp(log_temperature)
        total = 0.0
        for score, correct in usable:
            probability = pointwise_probability(score, temperature)
            probability = min(max(probability, 1e-15), 1 - 1e-15)
            total -= math.log(probability if correct else 1 - probability)
        return total / len(usable)

    low, high = -12.0, 12.0
    golden = (math.sqrt(5) - 1) / 2
    x1 = high - golden * (high - low)
    x2 = low + golden * (high - low)
    f1, f2 = objective(x1), objective(x2)
    for _ in range(120):
        if f1 > f2:
            low, x1, f1 = x1, x2, f2
            x2 = low + golden * (high - low)
            f2 = objective(x2)
        else:
            high, x2, f2 = x2, x1, f1
            x1 = high - golden * (high - low)
            f1 = objective(x1)
    return math.exp((low + high) / 2)


def calibrated_gate_features(
    record: dict[str, Any], calibration: dict[str, Any]
) -> dict[str, Any] | None:
    return gate_features(record, float(calibration["temperature"]))


def development_pairs(
    rows: Sequence[tuple[dict[str, Any], dict[str, Any]]],
    calibration: dict[str, Any],
) -> list[tuple[float, float, bool]]:
    pairs: list[tuple[float, float, bool]] = []
    for record, label in rows:
        features = calibrated_gate_features(record, calibration)
        if features is not None:
            pairs.append(
                (
                    features["top1_probability"],
                    features["margin"],
                    label_is_correct(record, label),
                )
            )
    return pairs


def metrics_for(
    rows: Sequence[tuple[dict[str, Any], dict[str, Any]]],
    calibration: dict[str, Any],
    threshold: float,
    margin_threshold: float,
    confidence: float,
) -> dict[str, Any]:
    pairs = development_pairs(rows, calibration)
    acted = [
        correct
        for probability, margin, correct in pairs
        if probability >= threshold and margin >= margin_threshold
    ]
    errors = sum(not correct for correct in acted)
    from _common import accuracy_lower_bound, risk_upper_bound

    return {
        "total_decisions": len(rows),
        "scoreable_non_tied": len(pairs),
        "acted_count": len(acted),
        "coverage": len(acted) / len(rows) if rows else 0.0,
        "errors": errors,
        "selective_risk": errors / len(acted) if acted else None,
        "accuracy_lower_bound": (
            accuracy_lower_bound(errors, len(acted), confidence) if acted else None
        ),
        "risk_upper_bound": risk_upper_bound(errors, len(acted), confidence) if acted else None,
        "confidence": confidence,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--split-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=50, help="Metadata only; not used by the transform")
    parser.add_argument("--risk-budget", type=float, default=0.02)
    parser.add_argument("--confidence", type=float, default=0.95)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.top_k < 2:
            raise HarnessError("--top-k must be at least 2")
        if not 0 < args.risk_budget < 1:
            raise HarnessError("--risk-budget must be between 0 and 1")
        if not 0 < args.confidence < 1:
            raise HarnessError("--confidence must be between 0 and 1")
        corpus = load_corpus(args.corpus)
        labels = load_labels(args.labels, corpus)
        primary = primary_labels(labels)
        split = load_split(args.split_manifest)
        if split.get("corpus_hash") != sha256_file(args.corpus):
            raise SplitGuardError("split manifest corpus hash does not match corpus")
        if split.get("labels_hash") != sha256_file(args.labels):
            raise SplitGuardError("split manifest labels hash does not match labels")
        development_ids = set(split["development_ids"])
        heldout_ids = set(split["heldout_ids"])
        if development_ids & heldout_ids:
            raise SplitGuardError("development and held-out ids overlap")
        if development_ids | heldout_ids != set(primary):
            raise SplitGuardError(
                "split manifest must partition every primary-labelled record exactly once"
            )
        missing_labels = (development_ids | heldout_ids) - set(primary)
        if missing_labels:
            raise SplitGuardError(
                f"split contains records without primary labels: {', '.join(sorted(missing_labels))}"
            )
        by_id = {record["record_id"]: record for record in corpus}
        missing_records = (development_ids | heldout_ids) - set(by_id)
        if missing_records:
            raise SplitGuardError(
                f"split contains records missing from corpus: {', '.join(sorted(missing_records))}"
            )
        development = [
            (dict(by_id[record_id], _split="development"), dict(primary[record_id]))
            for record_id in sorted(development_ids)
        ]
        heldout = [
            (dict(by_id[record_id], _split="heldout"), dict(primary[record_id]))
            for record_id in sorted(heldout_ids)
        ]
        if not development:
            raise SplitGuardError("development split is empty; refusing to fit held-out data")

        temperature = fit_temperature(development, args.top_k)
        calibration_base: dict[str, Any] = {
            "temperature": temperature,
            "top_k": args.top_k,
        }
        development_gate_pairs = development_pairs(development, calibration_base)
        chosen, surface, evaluated_points = choose_dual_operating_point(
            development_gate_pairs, len(development), args.risk_budget, args.confidence
        )
        calibration = {
            "schema": "eljev.eval.calibration/1",
            "calibration_version": "calibration-v1",
            "fit_date": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "corpus_hash": sha256_file(args.corpus),
            "labels_hash": sha256_file(args.labels),
            "split_hash": sha256_file(args.split_manifest),
            "split_manifest": str(args.split_manifest),
            "split_sizes": {
                "development": len(development),
                "heldout": len(heldout),
                "labelled": len(development) + len(heldout),
            },
            "temperature": temperature,
            "top_k": args.top_k,
            "risk_budget": args.risk_budget,
            "confidence": args.confidence,
            "threshold": chosen["threshold"],
            "margin_threshold": chosen["margin_threshold"],
            "metrics": {
                "development": metrics_for(
                    development,
                    calibration_base,
                    chosen["threshold"],
                    chosen["margin_threshold"],
                    args.confidence,
                ),
                "selected_development_operating_point": chosen,
                "development_surface": surface,
                "surface_points_evaluated": evaluated_points,
                "heldout_is_locked": True,
            },
        }
        write_json(args.output, calibration)
        print(
            f"wrote {args.output}; temperature={temperature:.8g} "
            f"threshold={chosen['threshold']:.8g} "
            f"margin_threshold={chosen['margin_threshold']:.8g}; "
            "held-out rows were not fitted"
        )
        return 0
    except (HarnessError, SplitGuardError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
