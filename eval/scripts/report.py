"""Generate a Markdown evaluation report from the locked held-out results."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from _common import (
    HarnessError,
    load_corpus,
    load_labels,
    percentile,
    primary_labels,
    read_json,
    retest_agreement,
    sha256_file,
    tier_name,
    utc_now,
    write_json,
)


LATENCY_BASELINE = [
    ("10 candidates", 606, 165, 669),
    ("50 candidates", 518, 183, 409),
    ("250 candidates", 1445, 334, 507),
    ("50 long (~2k chars)", 1190, 624, 1192),
]


def fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def pct(value: Any) -> str:
    return "n/a" if value is None else f"{float(value):.2%}"


def corpus_summary(corpus: list[dict[str, Any]]) -> dict[str, Any]:
    wall_times = [float(row["task_wall_ms"]) for row in corpus]
    sources = sorted(
        {
            str(row.get("provenance", {}).get("source"))
            for row in corpus
            if isinstance(row.get("provenance"), dict) and row.get("provenance", {}).get("source")
        }
    )
    policies = sorted(
        {
            str(row.get("redaction_policy", {}).get("name"))
            for row in corpus
            if isinstance(row.get("redaction_policy"), dict)
        }
    )
    return {
        "count": len(corpus),
        "sources": sources or ["unspecified"],
        "redaction_policies": policies or ["unspecified"],
        "wall_p50_ms": percentile(wall_times, 0.50),
        "wall_p95_ms": percentile(wall_times, 0.95),
        "min_captured_at": min((str(row.get("captured_at", "")) for row in corpus), default=""),
        "max_captured_at": max((str(row.get("captured_at", "")) for row in corpus), default=""),
        "tiers": sorted({tier_name(row) for row in corpus}),
    }


def render_curve(curve: list[dict[str, Any]]) -> list[str]:
    lines = [
        "| Margin threshold | Best threshold | Coverage | Acted | Errors | Point risk | Risk upper bound | Accuracy lower bound | Feasible |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    for point in curve:
        lines.append(
            f"| {point['margin_threshold']:.8g} | {point['best_threshold']:.8g} | "
            f"{pct(point['coverage'])} | "
            f"{point['acted_count']} | {point['errors']} | {pct(point['selective_risk'])} | "
            f"{pct(point['risk_upper_bound'])} | {pct(point['accuracy_lower_bound'])} | "
            f"{'yes' if point['feasible'] else 'no'} |"
        )
    return lines


def render_report(
    corpus: list[dict[str, Any]],
    labels: list[dict[str, Any]],
    split: dict[str, Any],
    calibration: dict[str, Any],
    curve_result: dict[str, Any],
    corpus_path: Path,
    labels_path: Path,
) -> str:
    summary = corpus_summary(corpus)
    agreement = retest_agreement(labels)
    operating = curve_result["operating_point"]
    risk_pass = (
        operating["risk_upper_bound"] is not None
        and operating["risk_upper_bound"] <= curve_result["risk_budget"]
    )
    coverage_pass = operating["coverage"] >= curve_result["minimum_coverage"]
    overall_pass = risk_pass and coverage_pass
    status = "PASS" if overall_pass else "FAIL"
    warning = curve_result.get("coverage_warning")
    lines = [
        "# el-jev Evaluation Report",
        "",
        f"Generated: `{utc_now()}`",
        "",
        f"## Result: **{status}**",
        "",
        f"- Statistical gate: **{'PASS' if risk_pass else 'FAIL'}** "
        f"(one-sided risk upper bound {pct(operating['risk_upper_bound'])} "
        f"<= budget {pct(curve_result['risk_budget'])}).",
        f"- Coverage usability: **{'PASS' if coverage_pass else 'FAIL'}** "
        f"({pct(operating['coverage'])} achieved; configured minimum "
        f"{pct(curve_result['minimum_coverage'])}).",
        "- Task-level latency criterion: **not assessed by this harness**; use the S1/S7 paired task measurement.",
        "",
    ]
    if warning:
        lines.extend([f"> **{warning}**", ""])
    lines.extend(
        [
            "## Corpus and provenance",
            "",
            f"- Corpus records: **{summary['count']}**.",
            f"- Corpus file: `{corpus_path}`.",
            f"- Corpus SHA-256: `{sha256_file(corpus_path)}`.",
            f"- Labels file: `{labels_path}`.",
            f"- Capture window: `{summary['min_captured_at']}` to `{summary['max_captured_at']}`.",
            f"- Sources: {', '.join(f'`{source}`' for source in summary['sources'])}.",
            f"- Redaction policies: {', '.join(f'`{policy}`' for policy in summary['redaction_policies'])}.",
            f"- Recorded task wall time: p50 **{fmt(summary['wall_p50_ms'])} ms**, "
            f"p95 **{fmt(summary['wall_p95_ms'])} ms**.",
            f"- Tiers present: {', '.join(f'`{tier}`' for tier in summary['tiers'])}.",
            "",
            "Raw corpora and labels are expected outside the repository under `~/.eljev/corpus`.",
            "",
            "## Split and labelling",
            "",
            f"- Development: **{split['split_sizes']['development']}**.",
            f"- Held-out: **{split['split_sizes']['heldout']}**.",
            f"- Primary-labelled total: **{split['split_sizes']['labelled']}**.",
            f"- Test–retest labelled records: **{agreement['records_with_multiple_labels']}**.",
            f"- Test–retest pairs: **{agreement['pairs']}**.",
            f"- Exact set agreement: **{pct(agreement['exact_set_agreement'])}**.",
            f"- Mean set Jaccard: **{pct(agreement['mean_jaccard'])}**.",
            "",
            "## Calibration",
            "",
            f"- Calibration version: `{calibration['calibration_version']}`.",
            f"- Fit date: `{calibration['fit_date']}`.",
            f"- Temperature: **{calibration['temperature']:.8g}**.",
            f"- Top-k: **{calibration['top_k']}**.",
            "- Transform: `sigmoid(logit(clip(score)) / temperature)` applied independently to top1/top2.",
            f"- Development threshold selected at risk budget: **{calibration['threshold']:.8g}**.",
            f"- Development margin threshold selected at risk budget: **{calibration['margin_threshold']:.8g}**.",
            "",
            "## Held-out coverage/risk curve",
            "",
            "Each row is the best score threshold at that margin threshold. The fitter swept both dimensions. Risk is shown as an exact one-sided upper confidence bound.",
            "",
            *render_curve(curve_result["coverage_risk_surface"]),
            "",
            f"Surface points evaluated: **{curve_result['surface_points_evaluated']}**.",
            "",
            "## Chosen operating point",
            "",
            f"- Threshold: **{operating['threshold']:.8g}**.",
            f"- Margin threshold: **{operating['margin_threshold']:.8g}**.",
            f"- Coverage: **{pct(operating['coverage'])}** ({operating['acted_count']} of "
            f"{operating['total_decisions']} held-out decisions acted on).",
            f"- Point selective risk: **{pct(operating['selective_risk'])}**.",
            f"- One-sided {pct(curve_result['confidence'])} risk upper bound: **{pct(operating['risk_upper_bound'])}**.",
            f"- One-sided {pct(curve_result['confidence'])} accuracy lower bound: **{pct(operating['accuracy_lower_bound'])}**.",
            f"- Held-out errors among acted-on decisions: **{operating['errors']}**.",
            f"- Held-out exact top ties: **{curve_result['heldout']['exact_top_ties']}**; "
            f"single-candidate rows: **{curve_result['heldout']['single_candidate']}**.",
            "",
            "## Per-tier breakdown",
            "",
            "| Tier | Decisions | Scoreable non-tied | Acted | Coverage | Errors | Point risk | Risk upper bound |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for tier, values in sorted(curve_result.get("per_tier", {}).items()):
        lines.append(
            f"| `{tier}` | {values['total_decisions']} | {values['total_scoreable_non_tied']} | "
            f"{values['acted_count']} | "
            f"{pct(values['coverage'])} | {values['errors']} | {pct(values['selective_risk'])} | "
            f"{pct(values['risk_upper_bound'])} |"
        )
    lines.extend(
        [
            "",
            "## Measured latency context",
            "",
            "| Workload | Cold ms | p50 ms | p95 ms |",
            "|---|---:|---:|---:|",
        ]
    )
    for workload, cold, p50, p95 in LATENCY_BASELINE:
        lines.append(f"| {workload} | {cold} | {p50} | {p95} |")
    lines.extend(
        [
            "",
            "These are the measured Cohere/daemon context values from the execution plan. They are not a substitute for paired task-level S1/S7 evidence.",
            "",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--split-manifest", type=Path, required=True)
    parser.add_argument("--calibration", type=Path, required=True)
    parser.add_argument("--coverage-risk", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        corpus = load_corpus(args.corpus)
        labels = load_labels(args.labels, corpus)
        split = read_json(args.split_manifest)
        calibration = read_json(args.calibration)
        curve_result = read_json(args.coverage_risk)
        corpus_hash = sha256_file(args.corpus)
        labels_hash = sha256_file(args.labels)
        split_hash = sha256_file(args.split_manifest)
        if calibration.get("corpus_hash") != corpus_hash or calibration.get("labels_hash") != labels_hash:
            raise HarnessError("calibration hashes do not match the report inputs")
        if calibration.get("split_hash") != split_hash:
            raise HarnessError("calibration split hash does not match the report inputs")
        if (
            curve_result.get("corpus_hash") != corpus_hash
            or curve_result.get("labels_hash") != labels_hash
            or curve_result.get("split_hash") != split_hash
        ):
            raise HarnessError("coverage-risk hashes do not match the report inputs")
        report = render_report(
            corpus,
            labels,
            split,
            calibration,
            curve_result,
            args.corpus,
            args.labels,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8", newline="\n")
        print(f"wrote {args.output}")
        return 0
    except (HarnessError, KeyError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
