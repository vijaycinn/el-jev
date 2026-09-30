"""Input validation and normalization for the §6 caps."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .types import Candidate


MIN_CANDIDATES = 1
MAX_CANDIDATES = 250
MAX_CHARS_PER_CANDIDATE = 2_000
MAX_TOTAL_CHARS = 100_000


class InputValidationError(ValueError):
    """A request violates the input contract and must produce exit code 1."""

    def __init__(self, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.field = field


@dataclass(frozen=True, slots=True)
class ValidatedInput:
    criterion: str
    candidates: list[Candidate]


def validate_criterion(criterion: Any) -> str:
    if not isinstance(criterion, str):
        raise InputValidationError("criterion must be a string", field="criterion")
    if not criterion.strip():
        raise InputValidationError("criterion must not be empty or whitespace", field="criterion")
    return criterion


def normalize_candidates(candidates: Any) -> list[Candidate]:
    """Normalize object or string candidates without changing their order."""

    if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes, bytearray)):
        raise InputValidationError("candidates must be a list", field="candidates")

    count = len(candidates)
    if count < MIN_CANDIDATES or count > MAX_CANDIDATES:
        raise InputValidationError(
            f"candidate count must be between {MIN_CANDIDATES} and {MAX_CANDIDATES}",
            field="candidates",
        )

    normalized: list[Candidate] = []
    seen_ids: set[str] = set()
    total_chars = 0

    for index, raw in enumerate(candidates):
        if isinstance(raw, str):
            candidate_id = f"c{index}"
            text = raw
        elif isinstance(raw, Mapping):
            if "id" not in raw or "text" not in raw:
                raise InputValidationError(
                    f"candidate {index} must contain id and text", field="candidates"
                )
            candidate_id = raw["id"]
            text = raw["text"]
            if not isinstance(candidate_id, str) or not candidate_id.strip():
                raise InputValidationError(
                    f"candidate {index} id must be a non-empty string", field="candidates"
                )
        else:
            raise InputValidationError(
                f"candidate {index} must be a string or object", field="candidates"
            )

        if not isinstance(text, str):
            raise InputValidationError(
                f"candidate {index} text must be a string", field="candidates"
            )
        if not text.strip():
            raise InputValidationError(
                f"candidate {index} text must not be empty or whitespace", field="candidates"
            )
        if len(text) > MAX_CHARS_PER_CANDIDATE:
            raise InputValidationError(
                f"candidate {index} exceeds {MAX_CHARS_PER_CANDIDATE} characters",
                field="candidates",
            )
        if candidate_id in seen_ids:
            raise InputValidationError(
                f"duplicate candidate id: {candidate_id}", field="candidates"
            )

        seen_ids.add(candidate_id)
        total_chars += len(text)
        if total_chars > MAX_TOTAL_CHARS:
            raise InputValidationError(
                f"candidate text exceeds {MAX_TOTAL_CHARS} total characters",
                field="candidates",
            )
        normalized.append(Candidate(candidate_id, text))

    return normalized


def validate_request(criterion: Any, candidates: Any) -> ValidatedInput:
    return ValidatedInput(validate_criterion(criterion), normalize_candidates(candidates))


def validate_top_n(top_n: Any, n_candidates: int) -> int:
    if top_n is None:
        return min(10, n_candidates)
    if isinstance(top_n, bool) or not isinstance(top_n, int):
        raise InputValidationError("top_n must be an integer", field="top_n")
    if top_n < 1 or top_n > n_candidates:
        raise InputValidationError("top_n must be between 1 and candidate count", field="top_n")
    return top_n


validate_candidates = normalize_candidates
