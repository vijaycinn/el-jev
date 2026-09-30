"""Local, deterministic short-circuit checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .types import Candidate


@dataclass(frozen=True, slots=True)
class PregateResult:
    trivial: bool
    reason: str | None = None


def check_pregate(candidates: Iterable[Candidate]) -> PregateResult:
    """Treat a one-candidate request as trivial because no ranking is needed."""

    count = sum(1 for _ in candidates)
    if count == 1:
        return PregateResult(True, "single_candidate")
    return PregateResult(False, None)


def is_trivial(candidates: Iterable[Candidate]) -> bool:
    return check_pregate(candidates).trivial


def should_short_circuit(candidates: Iterable[Candidate]) -> bool:
    return is_trivial(candidates)


run_pregate = check_pregate
