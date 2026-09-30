"""Append-only JSONL decision logging outside the repository tree."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import hashlib
import json
import os
from pathlib import Path
import threading
from typing import Any

from .types import Candidate, DecisionRecord


def default_log_dir() -> Path:
    env_dir = os.environ.get("ELJEV_LOG_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    local_root = Path(__file__).resolve().parent.parent / ".eljev" / "logs"
    if local_root.parent.exists():
        return local_root
    return Path(r".eljev/logs")


def redact_enabled() -> bool:
    return os.environ.get("ELJEV_REDACT", "1") != "0"


class DecisionLog:
    """Thread-safe append-only decision log."""

    def __init__(self, directory: str | os.PathLike[str] | None = None) -> None:
        self.directory = Path(directory).expanduser() if directory is not None else default_log_dir()
        self.path = self.directory / "decisions.jsonl"
        self._lock = threading.Lock()

    def append(
        self,
        record: DecisionRecord | Mapping[str, Any],
        candidates: Iterable[Candidate] | None = None,
    ) -> None:
        payload = record.to_dict() if isinstance(record, DecisionRecord) else dict(record)
        if candidates is not None:
            candidate_list = list(candidates)
            if redact_enabled():
                payload["candidate_hashes"] = [
                    hashlib.sha256(candidate.text.encode("utf-8")).hexdigest()
                    for candidate in candidate_list
                ]
            else:
                payload["candidates"] = [candidate.to_dict() for candidate in candidate_list]
        line = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line)
                stream.write("\n")

    def read_last(self, limit: int = 100) -> list[dict[str, Any]]:
        if limit < 1 or not self.path.exists():
            return []
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        records: list[dict[str, Any]] = []
        for line in lines[-limit:]:
            try:
                decoded = json.loads(line)
            except ValueError:
                continue
            if isinstance(decoded, dict):
                records.append(decoded)
        return records
