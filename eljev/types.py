"""Data types and JSON serialization for ``eljev.decision/1``."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from typing import Any, Mapping
from uuid import uuid4


SCHEMA = "eljev.decision/1"


def utc_timestamp() -> str:
    """Return a UTC timestamp with millisecond precision."""

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True, slots=True)
class Candidate:
    """A candidate in the original caller-supplied order."""

    id: str
    text: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "text": self.text}


@dataclass(frozen=True, slots=True)
class RankedResult:
    """A ranked engine result retaining the original candidate index."""

    id: str
    index: int
    relevance_score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "index": self.index,
            "relevance_score": self.relevance_score,
        }


@dataclass(slots=True)
class DecisionRecord:
    """The wire-format decision record emitted by the daemon."""

    schema: str = SCHEMA
    decision_id: str = field(default_factory=lambda: str(uuid4()))
    ts: str = field(default_factory=utc_timestamp)
    shape: str = "screen"
    criterion: str = ""
    n_candidates: int = 0
    tier_path: list[str] = field(default_factory=list)
    engine: str | None = None
    engine_version: str = "1"
    choice: str | None = None
    choice_index: int | None = None
    status: str = "needs_review"
    exit_code: int = 2
    raw_top_score: float | None = None
    raw_runner_up: float | None = None
    margin_raw: float | None = None
    calibrated_probability: float | None = None
    margin_calibrated: float | None = None
    calibration_version: str = "none"
    coverage_policy: str = "always_abstain_v0"
    results: list[dict[str, Any]] = field(default_factory=list)
    elapsed_ms: dict[str, float] = field(
        default_factory=lambda: {"total": 0.0, "pregate": 0.0, "engine": 0.0, "overhead": 0.0}
    )
    escalation_reason: str | None = None
    error_kind: str | None = None
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible dictionary in contract field order."""

        return {
            "schema": self.schema,
            "decision_id": self.decision_id,
            "ts": self.ts,
            "shape": self.shape,
            "criterion": self.criterion,
            "n_candidates": self.n_candidates,
            "tier_path": list(self.tier_path),
            "engine": self.engine,
            "engine_version": self.engine_version,
            "choice": self.choice,
            "choice_index": self.choice_index,
            "status": self.status,
            "exit_code": self.exit_code,
            "raw_top_score": self.raw_top_score,
            "raw_runner_up": self.raw_runner_up,
            "margin_raw": self.margin_raw,
            "calibrated_probability": self.calibrated_probability,
            "margin_calibrated": self.margin_calibrated,
            "calibration_version": self.calibration_version,
            "coverage_policy": self.coverage_policy,
            "results": [dict(result) for result in self.results],
            "elapsed_ms": dict(self.elapsed_ms),
            "escalation_reason": self.escalation_reason,
            "error_kind": self.error_kind,
            "notes": list(self.notes),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, allow_nan=False)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DecisionRecord":
        """Create a record from a decoded contract payload."""

        if not isinstance(value, Mapping):
            raise TypeError("decision record must be a mapping")
        fields = {
            "schema",
            "decision_id",
            "ts",
            "shape",
            "criterion",
            "n_candidates",
            "tier_path",
            "engine",
            "engine_version",
            "choice",
            "choice_index",
            "status",
            "exit_code",
            "raw_top_score",
            "raw_runner_up",
            "margin_raw",
            "calibrated_probability",
            "margin_calibrated",
            "calibration_version",
            "coverage_policy",
            "results",
            "elapsed_ms",
            "escalation_reason",
            "error_kind",
            "notes",
        }
        kwargs = {key: value[key] for key in fields if key in value}
        kwargs.setdefault("schema", SCHEMA)
        kwargs["tier_path"] = list(kwargs.get("tier_path", []))
        kwargs["results"] = [dict(item) for item in kwargs.get("results", [])]
        kwargs["elapsed_ms"] = dict(kwargs.get("elapsed_ms", {}))
        kwargs["notes"] = list(kwargs.get("notes", []))
        return cls(**kwargs)

    @classmethod
    def from_json(cls, payload: str | bytes) -> "DecisionRecord":
        return cls.from_dict(json.loads(payload))


def candidates_to_dict(candidates: list[Candidate]) -> list[dict[str, str]]:
    return [candidate.to_dict() for candidate in candidates]


Decision = DecisionRecord
EngineResult = RankedResult
