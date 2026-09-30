"""Local HTTP daemon for el-jev.

Remote engine calls are serialized by one bounded semaphore. Validation, pregate,
health, and log requests do not acquire it, so a slow paid call cannot starve
local work or health probes.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from typing import Any

from .decisionlog import DecisionLog
from .engines.cohere import CohereClient, CohereError
from .engines.systemone import SystemOneClient, SystemOneEngine, SystemOneError
from .pregate import check_pregate
from .types import Candidate, DecisionRecord, SCHEMA
from .validate import InputValidationError, validate_request, validate_top_n
from .verdict import verdict_from_engine


VERSION = "0.1.0"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787


def load_calibration() -> dict[str, Any] | None:
    """Load the optional evaluation artifact without allowing it to crash a request."""

    path = Path(__file__).resolve().parent.parent / "eval" / "calibration.json"
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _elapsed(total_start: float, pregate_start: float, engine_start: float | None, engine_end: float | None) -> dict[str, float]:
    now = time.perf_counter()
    total = (now - total_start) * 1000.0
    pregate = (engine_start - pregate_start) * 1000.0 if engine_start is not None else total
    engine = (
        (engine_end - engine_start) * 1000.0
        if engine_start is not None and engine_end is not None
        else 0.0
    )
    return {
        "total": round(total, 3),
        "pregate": round(max(0.0, pregate), 3),
        "engine": round(max(0.0, engine), 3),
        "overhead": round(max(0.0, total - pregate - engine), 3),
    }


class EljevHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler]) -> None:
        if server_address[0] != DEFAULT_HOST:
            raise ValueError("el-jev must bind to 127.0.0.1 only")
        super().__init__(server_address, handler_class)
        self.started_at = time.monotonic()
        self.engine_gate = threading.BoundedSemaphore(1)
        self.cohere = CohereClient()
        self.systemone = SystemOneEngine(cohere=self.cohere)
        self.decision_log = DecisionLog()


class EljevRequestHandler(BaseHTTPRequestHandler):
    server_version = "eljev/0.1.0"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> EljevHTTPServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, value: Mapping[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Any:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length or "0")
        except ValueError as exc:
            raise InputValidationError("Content-Length must be an integer") from exc
        if length < 0 or length > 2_000_000:
            raise InputValidationError("request body is too large")
        body = self.rfile.read(length)
        try:
            return json.loads(body)
        except (TypeError, ValueError) as exc:
            raise InputValidationError("request body must be valid JSON") from exc

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            self._health()
        elif parsed.path == "/v1/log":
            values = parse_qs(parsed.query)
            try:
                limit = int(values.get("limit", ["100"])[0])
            except ValueError:
                limit = 100
            self._send_json(200, {"records": self.app.decision_log.read_last(max(1, min(limit, 1000)))})
        else:
            self._send_json(404, {"error_kind": "http", "message": "not found"})

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/v1/screen":
            self._screen()
        elif parsed.path == "/v1/decide":
            self._decide()
        else:
            self._send_json(404, {"error_kind": "http", "message": "not found"})

    def _health(self) -> None:
        self._send_json(
            200,
            {
                "status": "ok",
                "version": VERSION,
                "schema": SCHEMA,
                "engines": {
                    "cohere": "ready" if self.app.cohere.endpoint else "absent",
                    "local": "ready"
                    if (self.app.systemone.passthrough_url or self.app.cohere.endpoint)
                    else "heuristic",
                },
                "calibration_version": "none",
                "coverage_policy": os.environ.get(
                    "ELJEV_COVERAGE_POLICY", "always_abstain_v0"
                ),
                "uptime_s": round(time.monotonic() - self.app.started_at, 3),
            },
        )

    def _screen(self) -> None:
        started = time.perf_counter()
        try:
            payload = self._read_json()
            if not isinstance(payload, Mapping):
                raise InputValidationError("request body must be an object")
            validated = validate_request(payload.get("criterion"), payload.get("candidates"))
            top_n = validate_top_n(payload.get("top_n"), len(validated.candidates))
            tier = payload.get("tier", "auto")
            if tier not in {"auto", "cohere", "local", "pregate_only"}:
                raise InputValidationError("tier must be auto, cohere, local, or pregate_only")
        except InputValidationError as exc:
            self._send_json(
                400,
                {"error_kind": "invalid_input", "message": str(exc), "exit_code": 1},
            )
            return

        candidates = validated.candidates
        pregate_result = check_pregate(candidates)
        pregate_end = time.perf_counter()
        policy = os.environ.get("ELJEV_COVERAGE_POLICY", "always_abstain_v0")
        if pregate_result.trivial:
            record = DecisionRecord(
                shape="screen",
                criterion=validated.criterion,
                n_candidates=len(candidates),
                tier_path=["pregate"],
                status="trivial",
                exit_code=2,
                escalation_reason=None,
                notes=[pregate_result.reason or "local pre-gate"],
                elapsed_ms=_elapsed(started, started, pregate_end, pregate_end),
                coverage_policy=policy,
            )
            self.app.decision_log.append(record, candidates)
            self._send_json(200, record.to_dict())
            return

        if tier == "pregate_only":
            record = DecisionRecord(
                shape="screen",
                criterion=validated.criterion,
                n_candidates=len(candidates),
                tier_path=["pregate"],
                status="needs_review",
                exit_code=2,
                escalation_reason="n_gt_local_cap",
                notes=["pregate_only requested but request was non-trivial"],
                elapsed_ms=_elapsed(started, started, pregate_end, pregate_end),
                coverage_policy=policy,
            )
            self.app.decision_log.append(record, candidates)
            self._send_json(200, record.to_dict())
            return

        if tier == "local":
            record = DecisionRecord(
                shape="screen",
                criterion=validated.criterion,
                n_candidates=len(candidates),
                tier_path=["pregate", "local"],
                status="engine_error",
                exit_code=2,
                error_kind="daemon_unavailable",
                notes=["local screen engine is not configured"],
                elapsed_ms=_elapsed(started, started, pregate_end, pregate_end),
                coverage_policy=policy,
            )
            self.app.decision_log.append(record, candidates)
            self._send_json(200, record.to_dict())
            return

        if not self.app.cohere.endpoint:
            record = DecisionRecord(
                shape="screen",
                criterion=validated.criterion,
                n_candidates=len(candidates),
                tier_path=["pregate", "cohere"],
                engine="cohere-rerank-v4.0-pro",
                status="engine_error",
                exit_code=2,
                error_kind="daemon_unavailable",
                notes=["ELJEV_COHERE_ENDPOINT is not configured"],
                elapsed_ms=_elapsed(started, started, pregate_end, pregate_end),
                coverage_policy=policy,
            )
            self.app.decision_log.append(record, candidates)
            self._send_json(200, record.to_dict())
            return

        engine_started = time.perf_counter()
        try:
            with self.app.engine_gate:
                response = self.app.cohere.rerank(
                    validated.criterion, [candidate.text for candidate in candidates], top_n
                )
            engine_ended = time.perf_counter()
        except CohereError as exc:
            engine_ended = time.perf_counter()
            malformed = exc.error_kind == "malformed"
            record = DecisionRecord(
                shape="screen",
                criterion=validated.criterion,
                n_candidates=len(candidates),
                tier_path=["pregate", "cohere"],
                engine="cohere-rerank-v4.0-pro",
                status="invalid_response" if malformed else "engine_error",
                exit_code=2,
                error_kind=exc.error_kind,
                notes=[
                    "Cohere returned a malformed response"
                    if malformed
                    else "Cohere engine call failed"
                ],
                elapsed_ms=_elapsed(started, started, engine_started, engine_ended),
                coverage_policy=policy,
            )
            self.app.decision_log.append(record, candidates)
            self._send_json(200, record.to_dict())
            return

        verdict = verdict_from_engine(
            response,
            candidates,
            policy=policy,
            calibration=load_calibration() if policy == "calibrated" else None,
            top_n=top_n,
        )
        record = DecisionRecord(
            shape="screen",
            criterion=validated.criterion,
            n_candidates=len(candidates),
            tier_path=["pregate", "cohere"],
            engine="cohere-rerank-v4.0-pro",
            choice=verdict["choice"],
            choice_index=verdict["choice_index"],
            status=verdict["status"],
            exit_code=verdict["exit_code"],
            raw_top_score=verdict["raw_top_score"],
            raw_runner_up=verdict["raw_runner_up"],
            margin_raw=verdict["margin_raw"],
            calibrated_probability=verdict["calibrated_probability"],
            margin_calibrated=verdict["margin_calibrated"],
            calibration_version=verdict["calibration_version"],
            coverage_policy=verdict["coverage_policy"],
            results=verdict["results"],
            elapsed_ms=_elapsed(started, started, engine_started, engine_ended),
            escalation_reason=verdict["escalation_reason"],
            error_kind=verdict["error_kind"],
            notes=verdict["notes"],
        )
        self.app.decision_log.append(record, candidates)
        self._send_json(200, record.to_dict())

    def _decide(self) -> None:
        try:
            payload = self._read_json()
            if not isinstance(payload, Mapping):
                raise InputValidationError("request body must be an object")
            state = payload.get("state")
            question = payload.get("question")
            if not isinstance(state, (str, Mapping, list)) or (isinstance(state, str) and not state.strip()):
                raise InputValidationError("state must be a non-empty string or JSON object")
            if not isinstance(question, Mapping):
                raise InputValidationError("question must be an object")
            question_id = question.get("id")
            question_kind = (question.get("kind") or question.get("type") or "").lower()
            if not isinstance(question_id, str) or not question_id:
                raise InputValidationError("question.id must be a non-empty string")
            if question_kind not in {"choice", "noul", "score"}:
                raise InputValidationError("question.kind must be choice, noul, or score")
            if question_kind == "choice" and not isinstance(question.get("options"), list) and not isinstance(question.get("criteria"), Mapping):
                raise InputValidationError("choice questions must contain an options list or criteria mapping")
            if question_kind == "score" and not isinstance(question.get("criteria"), Sequence):
                raise InputValidationError("score questions must contain a criteria list")
            result = self.app.systemone.decide(state, question)
        except InputValidationError as exc:
            self._send_json(400, {"error_kind": "invalid_input", "message": str(exc), "exit_code": 1})
            return
        except SystemOneError as exc:
            self._send_json(502, {"error_kind": exc.error_kind, "message": str(exc)})
            return
        self._send_json(200, result)


def create_server(
    host: str | None = None,
    port: int | None = None,
) -> EljevHTTPServer:
    """Create the daemon, refusing non-loopback binds."""

    bind_host = host or os.environ.get("ELJEV_HOST", DEFAULT_HOST)
    if bind_host != DEFAULT_HOST:
        raise ValueError("el-jev must bind to 127.0.0.1 only")
    bind_port = int(port if port is not None else os.environ.get("ELJEV_PORT", DEFAULT_PORT))
    return EljevHTTPServer((DEFAULT_HOST, bind_port), EljevRequestHandler)


def run(host: str | None = None, port: int | None = None) -> None:
    server = create_server(host, port)
    try:
        server.serve_forever()
    finally:
        server.cohere.close()
        server.server_close()
