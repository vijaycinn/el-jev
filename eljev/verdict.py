"""Pure engine-result validation, ranking, tie handling, and coverage policy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import Any

from .types import Candidate


def _candidate_id(candidate: Candidate | Mapping[str, Any]) -> str:
    return candidate.id if isinstance(candidate, Candidate) else str(candidate["id"])


def _finite_score(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and 0.0 <= float(value) <= 1.0
    )


def validate_engine_response(response: Any, n_candidates: int) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Validate the provider shape before any ranking operation occurs."""

    if not isinstance(response, Mapping):
        return None, "response must be an object"
    results = response.get("results")
    if not isinstance(results, list) or not results:
        return None, "results must be a non-empty list"

    validated: list[dict[str, Any]] = []
    seen_indices: set[int] = set()
    for position, item in enumerate(results):
        if not isinstance(item, Mapping):
            return None, f"result {position} must be an object"
        index = item.get("index")
        score = item.get("relevance_score")
        if isinstance(index, bool) or not isinstance(index, int):
            return None, f"result {position} index must be an integer"
        if index < 0 or index >= n_candidates:
            return None, f"result {position} index is out of range"
        if index in seen_indices:
            return None, f"result {position} duplicates index {index}"
        if not _finite_score(score):
            return None, f"result {position} relevance_score must be finite and in [0, 1]"
        seen_indices.add(index)
        validated.append({"index": index, "relevance_score": float(score)})
    return validated, None


def _invalid_verdict(message: str, policy: str) -> dict[str, Any]:
    return {
        "choice": None,
        "choice_index": None,
        "status": "invalid_response",
        "exit_code": 2,
        "error_kind": "malformed",
        "raw_top_score": None,
        "raw_runner_up": None,
        "margin_raw": None,
        "calibrated_probability": None,
        "margin_calibrated": None,
        "calibration_version": "none",
        "coverage_policy": policy,
        "results": [],
        "escalation_reason": None,
        "notes": [message],
    }


def _valid_calibration(calibration: Any) -> tuple[float, float, float, str] | None:
    if not isinstance(calibration, Mapping):
        return None
    temperature = calibration.get("temperature")
    threshold = calibration.get("threshold")
    margin_threshold = calibration.get("margin_threshold")
    calibration_version = calibration.get("calibration_version")
    if (
        isinstance(temperature, bool)
        or not isinstance(temperature, (int, float))
        or not math.isfinite(float(temperature))
        or float(temperature) <= 0
        or isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not math.isfinite(float(threshold))
        or not 0.0 <= float(threshold) <= 1.0
        or isinstance(margin_threshold, bool)
        or not isinstance(margin_threshold, (int, float))
        or not math.isfinite(float(margin_threshold))
        or not 0.0 <= float(margin_threshold) <= 1.0
        or not isinstance(calibration_version, str)
        or not calibration_version
    ):
        return None
    return (
        float(temperature),
        float(threshold),
        float(margin_threshold),
        calibration_version,
    )


def _temperature_scale(score: float, temperature: float) -> float:
    clipped = min(max(score, 1e-12), 1.0 - 1e-12)
    logit = math.log(clipped / (1.0 - clipped))
    scaled = logit / temperature
    if scaled >= 0:
        return 1.0 / (1.0 + math.exp(-scaled))
    exp_scaled = math.exp(scaled)
    return exp_scaled / (1.0 + exp_scaled)


def verdict_from_engine(
    response: Any,
    candidates: Sequence[Candidate | Mapping[str, Any]],
    *,
    policy: str = "always_abstain_v0",
    calibration: Mapping[str, Any] | None = None,
    top_n: int | None = None,
) -> dict[str, Any]:
    """Turn a provider response into a fail-closed, policy-aware verdict."""

    validated, error = validate_engine_response(response, len(candidates))
    if error is not None or validated is None:
        return _invalid_verdict(error or "invalid response", policy)

    ranked = sorted(
        validated,
        key=lambda item: (-float(item["relevance_score"]), int(item["index"])),
    )
    limit = len(ranked) if top_n is None else max(0, min(int(top_n), len(ranked)))
    visible = ranked[:limit]
    result_dicts = [
        {
            "id": _candidate_id(candidates[item["index"]]),
            "index": item["index"],
            "relevance_score": item["relevance_score"],
        }
        for item in visible
    ]

    top = ranked[0]
    runner_up = ranked[1] if len(ranked) > 1 else None
    top_score = float(top["relevance_score"])
    runner_score = float(runner_up["relevance_score"]) if runner_up else None
    margin_raw = top_score - runner_score if runner_score is not None else None

    common = {
        "choice": _candidate_id(candidates[top["index"]]),
        "choice_index": int(top["index"]),
        "raw_top_score": top_score,
        "raw_runner_up": runner_score,
        "margin_raw": margin_raw,
        "results": result_dicts,
        "escalation_reason": None,
        "notes": [],
        "error_kind": None,
        "coverage_policy": policy,
        "calibration_version": "none",
        "calibrated_probability": None,
        "margin_calibrated": None,
    }

    if runner_up is not None and top_score == runner_score:
        common.update(
            choice=None,
            choice_index=None,
            status="abstain_tie",
            exit_code=2,
            notes=["top two relevance scores are exactly equal"],
        )
        return common

    if policy == "calibrated":
        parsed_calibration = _valid_calibration(calibration)
        if parsed_calibration is None:
            common.update(
                status="needs_review",
                exit_code=2,
                notes=["calibrated coverage unavailable: calibration is missing or invalid"],
            )
            return common
        temperature, threshold, margin_threshold, version = parsed_calibration
        calibrated_top = _temperature_scale(top_score, temperature)
        calibrated_runner = (
            _temperature_scale(runner_score, temperature) if runner_score is not None else 0.0
        )
        calibrated_margin = calibrated_top - calibrated_runner
        common.update(
            calibration_version=version,
            calibrated_probability=calibrated_top,
            margin_calibrated=calibrated_margin,
        )
        probability_passes = calibrated_top >= threshold
        margin_passes = calibrated_margin >= margin_threshold
        if runner_up is None:
            common["notes"] = [
                "calibrated margin term was vacuous: no runner-up exists"
            ]
        if not probability_passes:
            common["notes"].append("calibrated probability below threshold")
        if not margin_passes:
            common["notes"].append("calibrated margin below margin_threshold")
        if runner_up is not None and probability_passes and margin_passes:
            common.update(status="selected", exit_code=0)
        else:
            common.update(status="needs_review", exit_code=2)
        return common

    if policy != "always_abstain_v0":
        common["notes"] = [f"unknown coverage policy {policy!r}; abstaining"]
    common.update(status="needs_review", exit_code=2)
    return common


def score_to_verdict(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return verdict_from_engine(*args, **kwargs)


def evaluate_results(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return verdict_from_engine(*args, **kwargs)


build_verdict = verdict_from_engine
