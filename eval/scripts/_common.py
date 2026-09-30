"""Shared standard-library helpers for the el-jev evaluation harness."""

from __future__ import annotations

import hashlib
import itertools
import json
import math
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
EVAL_DIR = SCRIPT_DIR.parent
REPO_ROOT = EVAL_DIR.parent
DEFAULT_CORPUS_DIR = Path.home() / ".eljev" / "corpus"
DEFAULT_CORPUS = DEFAULT_CORPUS_DIR / "decisions.jsonl"
DEFAULT_LABELS = DEFAULT_CORPUS_DIR / "labels.jsonl"
DEFAULT_REPORT_DIR = EVAL_DIR / "reports"
_EPSILON = 1e-12


class HarnessError(ValueError):
    """A user-facing validation or data-integrity error."""


class SplitGuardError(HarnessError):
    """Raised when a fitting operation could include held-out data."""


def fail(message: str) -> "NoReturn":
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(2)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise HarnessError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(value, dict):
                raise HarnessError(f"{path}:{line_number}: each JSONL record must be an object")
            rows.append(value)
    return rows


def write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    ensure_parent(path)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
            handle.write("\n")


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HarnessError(f"{path}: invalid JSON: {exc}") from exc


def write_json(path: Path, value: Any) -> None:
    ensure_parent(path)
    path.write_text(
        json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def reject_repository_corpus_path(path: Path) -> None:
    if is_under(path, EVAL_DIR / "corpus"):
        raise HarnessError(
            f"refusing to write real data under {EVAL_DIR / 'corpus'}; "
            "use the default external path under ~/.eljev/corpus"
        )


def parse_float(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HarnessError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise HarnessError(f"{field} must be finite")
    return result


def candidate_ids(record: Mapping[str, Any]) -> list[str]:
    ids = record.get("candidate_ids")
    if isinstance(ids, list) and all(isinstance(item, str) for item in ids):
        return list(ids)
    candidates = record.get("candidate_payload", record.get("candidates"))
    if isinstance(candidates, list):
        result = []
        for candidate in candidates:
            if not isinstance(candidate, Mapping) or not isinstance(candidate.get("id"), str):
                raise HarnessError(f"{record.get('record_id', '<record>')}: candidate ids are invalid")
            result.append(candidate["id"])
        return result
    raise HarnessError(f"{record.get('record_id', '<record>')}: missing candidate ids")


def validate_corpus_record(record: Mapping[str, Any]) -> None:
    required = (
        "record_id",
        "criterion",
        "candidate_payload",
        "candidate_ids",
        "n_candidates",
        "candidate_lengths",
        "baseline_choice",
        "deterministic_trigger",
        "task_wall_ms",
    )
    missing = [field for field in required if field not in record]
    if missing:
        raise HarnessError(f"{record.get('record_id', '<record>')}: missing fields: {', '.join(missing)}")
    if not isinstance(record["record_id"], str) or not record["record_id"]:
        raise HarnessError("record_id must be a non-empty string")
    if not isinstance(record["criterion"], str) or not record["criterion"].strip():
        raise HarnessError(f"{record['record_id']}: criterion must be non-empty")
    candidates = record["candidate_payload"]
    if not isinstance(candidates, list) or not candidates:
        raise HarnessError(f"{record['record_id']}: candidate_payload must be a non-empty array")
    ids = candidate_ids(record)
    if len(ids) != len(set(ids)):
        raise HarnessError(f"{record['record_id']}: candidate ids must be unique")
    if record["n_candidates"] != len(candidates) or record["n_candidates"] != len(ids):
        raise HarnessError(f"{record['record_id']}: n_candidates does not match candidate_payload")
    lengths = record["candidate_lengths"]
    if not isinstance(lengths, list) or len(lengths) != len(candidates):
        raise HarnessError(f"{record['record_id']}: candidate_lengths does not match candidates")
    for index, (candidate, candidate_id, length) in enumerate(zip(candidates, ids, lengths)):
        if not isinstance(candidate, Mapping) or candidate.get("id") != candidate_id:
            raise HarnessError(f"{record['record_id']}: candidate {index} id mismatch")
        if not isinstance(candidate.get("text"), str) or not candidate["text"].strip():
            raise HarnessError(f"{record['record_id']}: candidate {candidate_id} text must be non-empty")
        if not isinstance(length, int) or length != len(candidate["text"]):
            raise HarnessError(f"{record['record_id']}: candidate length mismatch for {candidate_id}")
    parse_float(record["task_wall_ms"], f"{record['record_id']}.task_wall_ms")
    if not isinstance(record["candidate_ids"], list):
        raise HarnessError(f"{record['record_id']}: candidate_ids must be an array")
    results = record.get("model_results")
    if results is not None:
        if not isinstance(results, list):
            raise HarnessError(f"{record['record_id']}: model_results must be an array")
        seen: set[str] = set()
        for result in results:
            if not isinstance(result, Mapping) or not isinstance(result.get("id"), str):
                raise HarnessError(f"{record['record_id']}: malformed model result")
            if result["id"] in seen:
                raise HarnessError(f"{record['record_id']}: duplicate model result id")
            seen.add(result["id"])
            if result["id"] not in ids:
                raise HarnessError(f"{record['record_id']}: model result id is not a candidate")
            parse_float(result.get("relevance_score"), f"{record['record_id']}.relevance_score")


_SECRET_KEY = re.compile(r"(?:token|secret|password|passwd|authorization|api[_-]?key|credential)", re.I)
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_LONG_SECRET = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")
_PHONE = re.compile(r"(?<!\d)(?:\+?\d[\d(). -]{8,}\d)(?!\d)")


def redact_text(text: str) -> str:
    """Apply the entry redaction policy once, deterministically."""
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _EMAIL.sub("[EMAIL]", text)
    text = _PHONE.sub("[PHONE]", text)
    return _LONG_SECRET.sub("[SECRET]", text)


def redact_value(value: Any, key: str | None = None) -> Any:
    if key and _SECRET_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, dict):
        return {str(k): redact_value(v, str(k)) for k, v in value.items()}
    return value


def redact_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(candidate.get("id"), str) or not candidate["id"]:
        raise HarnessError("each candidate needs a non-empty string id")
    if not isinstance(candidate.get("text"), str) or not candidate["text"].strip():
        raise HarnessError(f"candidate {candidate.get('id', '<unknown>')} has empty text")
    result = {str(key): redact_value(value, str(key)) for key, value in candidate.items()}
    result["id"] = candidate["id"]
    result["text"] = redact_text(candidate["text"])
    return result


def load_corpus(path: Path) -> list[dict[str, Any]]:
    rows = read_jsonl(path)
    seen: set[str] = set()
    for row in rows:
        validate_corpus_record(row)
        record_id = row["record_id"]
        if record_id in seen:
            raise HarnessError(f"{path}: duplicate record_id {record_id}")
        seen.add(record_id)
    if not rows:
        raise HarnessError(f"{path}: corpus is empty")
    return rows


def normalize_choices(value: Any, valid_ids: set[str]) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        if value.strip().lower() in {"", "none", "null", "no candidate"}:
            return []
        values = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, list):
        values = value
    else:
        raise HarnessError("acceptable_choices must be a list or comma-separated string")
    if any(not isinstance(item, str) or not item for item in values):
        raise HarnessError("acceptable_choices must contain non-empty strings")
    unique = list(dict.fromkeys(values))
    unknown = [item for item in unique if item not in valid_ids]
    if unknown:
        raise HarnessError(f"unknown candidate id(s): {', '.join(unknown)}")
    return unique


def validate_label(label: Mapping[str, Any], valid_ids: set[str] | None = None) -> None:
    required = ("annotation_id", "record_id", "labeler_id", "acceptable_choices", "annotation_kind")
    missing = [field for field in required if field not in label]
    if missing:
        raise HarnessError(f"label missing fields: {', '.join(missing)}")
    forbidden = {"baseline_choice", "model_results", "relevance_score", "raw_top_score", "raw_runner_up"}
    leaked = sorted(forbidden.intersection(label))
    if leaked:
        raise HarnessError(f"label contains blinded fields: {', '.join(leaked)}")
    if not isinstance(label["acceptable_choices"], list):
        raise HarnessError(f"{label['annotation_id']}: acceptable_choices must be an array")
    if bool(label.get("none")) != (len(label["acceptable_choices"]) == 0):
        raise HarnessError(
            f"{label['annotation_id']}: none must match whether acceptable_choices is empty"
        )
    if valid_ids is not None:
        normalize_choices(label["acceptable_choices"], valid_ids)
    if label["annotation_kind"] not in {"primary", "retest"}:
        raise HarnessError(f"{label['annotation_id']}: annotation_kind must be primary or retest")


def load_labels(path: Path, corpus: Sequence[Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    rows = read_jsonl(path)
    records_by_id = {row["record_id"]: row for row in corpus or []}
    seen: set[str] = set()
    for row in rows:
        record_id = row.get("record_id")
        valid_ids = None
        if record_id in records_by_id:
            valid_ids = set(candidate_ids(records_by_id[record_id]))
        validate_label(row, valid_ids)
        if row["annotation_id"] in seen:
            raise HarnessError(f"{path}: duplicate annotation_id {row['annotation_id']}")
        seen.add(row["annotation_id"])
    return rows


def primary_labels(labels: Sequence[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for label in labels:
        if label.get("annotation_kind", "primary") != "primary":
            continue
        record_id = label["record_id"]
        if record_id in result:
            raise HarnessError(f"multiple primary labels for {record_id}; keep retests marked retest")
        result[record_id] = label
    return result


def retest_agreement(labels: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_record: dict[str, list[set[str]]] = defaultdict(list)
    for label in labels:
        by_record[label["record_id"]].append(set(label["acceptable_choices"]))
    pairs: list[tuple[set[str], set[str]]] = []
    for values in by_record.values():
        pairs.extend(itertools.combinations(values, 2))
    if not pairs:
        return {
            "records_with_multiple_labels": 0,
            "pairs": 0,
            "exact_set_agreement": None,
            "mean_jaccard": None,
        }
    exact = sum(left == right for left, right in pairs) / len(pairs)
    jaccards: list[float] = []
    for left, right in pairs:
        union = left | right
        jaccards.append(1.0 if not union else len(left & right) / len(union))
    return {
        "records_with_multiple_labels": sum(len(values) > 1 for values in by_record.values()),
        "pairs": len(pairs),
        "exact_set_agreement": exact,
        "mean_jaccard": sum(jaccards) / len(jaccards),
    }


def load_split(path: Path) -> dict[str, Any]:
    value = read_json(path)
    if not isinstance(value, dict):
        raise HarnessError(f"{path}: split manifest must be an object")
    development = value.get("development_ids")
    heldout = value.get("heldout_ids")
    if not isinstance(development, list) or not isinstance(heldout, list):
        raise HarnessError(f"{path}: split manifest needs development_ids and heldout_ids")
    if set(development) & set(heldout):
        raise SplitGuardError(f"{path}: development and held-out ids overlap")
    if len(development) != len(set(development)) or len(heldout) != len(set(heldout)):
        raise HarnessError(f"{path}: split ids must be unique")
    return value


def attach_split(
    records: Sequence[Mapping[str, Any]],
    split: Mapping[str, Any],
    split_name: str,
) -> list[dict[str, Any]]:
    allowed = set(split.get(f"{split_name}_ids", []))
    return [dict(record, _split=split_name) for record in records if record["record_id"] in allowed]


def model_ranking(record: Mapping[str, Any], top_k: int | None = None) -> list[dict[str, Any]]:
    results = record.get("model_results")
    if not isinstance(results, list) or not results:
        return []
    normalized = [
        {
            "id": result["id"],
            "index": result.get("index"),
            "relevance_score": parse_float(result["relevance_score"], "relevance_score"),
        }
        for result in results
    ]
    normalized.sort(key=lambda item: item["relevance_score"], reverse=True)
    return normalized[:top_k] if top_k else normalized


def is_exact_top_tie(ranking: Sequence[Mapping[str, Any]]) -> bool:
    return len(ranking) >= 2 and ranking[0]["relevance_score"] == ranking[1]["relevance_score"]


def logit(probability: float) -> float:
    probability = min(max(probability, 1e-15), 1 - 1e-15)
    return math.log(probability / (1 - probability))


def sigmoid(value: float) -> float:
    if value >= 0:
        z = math.exp(-value)
        return 1 / (1 + z)
    z = math.exp(value)
    return z / (1 + z)


def pointwise_probability(score: float, temperature: float) -> float:
    """The binding §5.1 transform used by eljev/verdict.py."""
    if temperature <= 0 or not math.isfinite(temperature):
        raise HarnessError("temperature must be positive and finite")
    clipped = min(max(score, 1e-12), 1.0 - 1e-12)
    return sigmoid(math.log(clipped / (1.0 - clipped)) / temperature)


def gate_features(record: Mapping[str, Any], temperature: float) -> dict[str, Any] | None:
    """Return pointwise top-two probabilities, or None for a structural abstention."""
    ranking = model_ranking(record)
    if len(ranking) < 2 or is_exact_top_tie(ranking):
        return None
    top1 = pointwise_probability(ranking[0]["relevance_score"], temperature)
    top2 = pointwise_probability(ranking[1]["relevance_score"], temperature)
    return {
        "top1_probability": top1,
        "top2_probability": top2,
        "margin": top1 - top2,
        "top1_id": ranking[0]["id"],
        "top2_id": ranking[1]["id"],
    }


def label_is_correct(record: Mapping[str, Any], label: Mapping[str, Any]) -> bool:
    ranking = model_ranking(record)
    acceptable = set(label["acceptable_choices"])
    return bool(ranking) and bool(acceptable) and ranking[0]["id"] in acceptable


def beta_regularized(x: float, a: float, b: float) -> float:
    """Regularized incomplete beta using the Numerical Recipes continued fraction."""
    if not 0 <= x <= 1:
        raise ValueError("beta input must be in [0, 1]")
    if x == 0:
        return 0.0
    if x == 1:
        return 1.0
    if (
        abs(a - round(a)) < 1e-10
        and abs(b - round(b)) < 1e-10
        and a + b <= 10000
    ):
        # The confidence-bound calls use integer beta shapes. The binomial-tail
        # identity is simple and avoids numerical ambiguity in the continued fraction.
        integer_a = int(round(a))
        trials = int(round(a + b - 1))
        terms = []
        log_x = math.log(x)
        log_one_minus_x = math.log1p(-x)
        for successes in range(integer_a, trials + 1):
            log_term = (
                math.lgamma(trials + 1)
                - math.lgamma(successes + 1)
                - math.lgamma(trials - successes + 1)
                + successes * log_x
                + (trials - successes) * log_one_minus_x
            )
            terms.append(math.exp(log_term))
        return min(1.0, max(0.0, math.fsum(terms)))
    log_beta = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    def continued_fraction(aa: float, bb: float, z: float) -> float:
        qab = aa + bb
        qap = aa + 1.0
        qam = aa - 1.0
        c = 1.0
        d = 1.0 - qab * x / qap
        if abs(d) < 3e-30:
            d = 3e-30
        d = 1.0 / d
        h = d
        for iteration in range(1, 201):
            m2 = 2 * iteration
            numerator = iteration * (bb - iteration) * z / ((qam + m2) * (aa + m2))
            d = 1.0 + numerator * d
            if abs(d) < 3e-30:
                d = 3e-30
            c = 1.0 + numerator / c
            if abs(c) < 3e-30:
                c = 3e-30
            d = 1.0 / d
            h *= d * c
            numerator = -(aa + iteration) * (qab + iteration) * z / ((aa + m2) * (qap + m2))
            d = 1.0 + numerator * d
            if abs(d) < 3e-30:
                d = 3e-30
            c = 1.0 + numerator / c
            if abs(c) < 3e-30:
                c = 3e-30
            d = 1.0 / d
            delta = d * c
            h *= delta
            if abs(delta - 1.0) < 3e-7:
                break
        return h

    if x < (a + 1) / (a + b + 2):
        front = math.exp(a * math.log(x) + b * math.log1p(-x) - log_beta) / a
        return min(1.0, front * continued_fraction(a, b, x))
    complement_front = (
        math.exp(b * math.log1p(-x) + a * math.log(x) - log_beta) / b
    )
    complement = complement_front * continued_fraction(b, a, 1 - x)
    return max(0.0, 1.0 - complement)


def beta_quantile(probability: float, a: float, b: float) -> float:
    if not 0 < probability < 1:
        return 0.0 if probability <= 0 else 1.0
    low, high = 0.0, 1.0
    for _ in range(90):
        middle = (low + high) / 2
        if beta_regularized(middle, a, b) < probability:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def risk_upper_bound(errors: int, trials: int, confidence: float) -> float:
    if trials <= 0:
        return 0.0
    if errors < 0 or errors > trials:
        raise HarnessError("errors must be between zero and trials")
    alpha = 1.0 - confidence
    if errors == trials:
        return 1.0
    if errors == 0:
        return 1.0 - alpha ** (1.0 / trials)
    return beta_quantile(confidence, errors + 1, trials - errors)


def accuracy_lower_bound(errors: int, trials: int, confidence: float) -> float:
    return 1.0 - risk_upper_bound(errors, trials, confidence)


def _dual_point(
    threshold: float,
    margin_threshold: float,
    total_decisions: int,
    acted_count: int,
    errors: int,
    confidence: float,
    risk_budget: float,
) -> dict[str, Any]:
    point_risk = errors / acted_count if acted_count else None
    # A point estimate above budget cannot satisfy the confidence-bound gate.
    bound = (
        risk_upper_bound(errors, acted_count, confidence)
        if acted_count and point_risk is not None and point_risk <= risk_budget
        else None
    )
    return {
        "threshold": threshold,
        "margin_threshold": margin_threshold,
        "total_decisions": total_decisions,
        "acted_count": acted_count,
        "coverage": acted_count / total_decisions if total_decisions else 0.0,
        "errors": errors,
        "selective_risk": point_risk,
        "accuracy_lower_bound": 1.0 - bound if bound is not None else None,
        "risk_upper_bound": bound,
        "confidence": confidence,
        "feasible": bound is not None and bound <= risk_budget,
    }


def choose_dual_operating_point(
    probabilities_margins_and_correctness: Sequence[tuple[float, float, bool]],
    total_decisions: int,
    risk_budget: float,
    confidence: float,
) -> tuple[dict[str, Any], list[dict[str, Any]], int]:
    """Sweep p(top1) and p(top1)-p(top2), returning a margin surface envelope."""
    if not probabilities_margins_and_correctness:
        empty = _dual_point(1.0, 1.0, total_decisions, 0, 0, confidence, risk_budget)
        return empty, [], 0

    margin_values = sorted(
        {0.0, *(margin for _, margin, _ in probabilities_margins_and_correctness)},
        reverse=True,
    )
    global_best: dict[str, Any] | None = None
    surface: list[dict[str, Any]] = []
    evaluated_points = 0

    for margin_threshold in margin_values:
        eligible = [
            (probability, correct)
            for probability, margin, correct in probabilities_margins_and_correctness
            if margin >= margin_threshold
        ]
        eligible.sort(key=lambda item: item[0], reverse=True)
        best_feasible: dict[str, Any] | None = None
        max_coverage: dict[str, Any] | None = None
        index = 0
        acted_count = 0
        errors = 0
        while index < len(eligible):
            threshold = eligible[index][0]
            end = index
            while end < len(eligible) and eligible[end][0] == threshold:
                end += 1
            group = eligible[index:end]
            acted_count += len(group)
            errors += sum(not correct for _, correct in group)
            point = _dual_point(
                threshold,
                margin_threshold,
                total_decisions,
                acted_count,
                errors,
                confidence,
                risk_budget,
            )
            evaluated_points += 1
            if max_coverage is None or point["coverage"] > max_coverage["coverage"]:
                max_coverage = point
            if point["feasible"] and (
                best_feasible is None
                or (point["coverage"], -point["threshold"])
                > (best_feasible["coverage"], -best_feasible["threshold"])
            ):
                best_feasible = point
            index = end

        selected = best_feasible or max_coverage
        if selected is None:
            selected = _dual_point(
                1.0,
                margin_threshold,
                total_decisions,
                0,
                0,
                confidence,
                risk_budget,
            )
        surface.append(
            {
                "margin_threshold": margin_threshold,
                "best_threshold": selected["threshold"],
                "coverage": selected["coverage"],
                "acted_count": selected["acted_count"],
                "errors": selected["errors"],
                "selective_risk": selected["selective_risk"],
                "risk_upper_bound": selected["risk_upper_bound"],
                "accuracy_lower_bound": selected["accuracy_lower_bound"],
                "feasible": selected["feasible"],
                "max_coverage_point": max_coverage,
            }
        )
        if selected["feasible"] and (
            global_best is None
            or (selected["coverage"], -selected["threshold"], -selected["margin_threshold"])
            > (global_best["coverage"], -global_best["threshold"], -global_best["margin_threshold"])
        ):
            global_best = selected

    if global_best is None:
        global_best = _dual_point(1.0, 1.0, total_decisions, 0, 0, confidence, risk_budget)
    return global_best, surface, evaluated_points


def tier_name(record: Mapping[str, Any]) -> str:
    path = record.get("tier_path")
    if isinstance(path, list) and path:
        return str(path[-1])
    value = record.get("tier")
    return str(value) if value else "unknown"


def percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]
