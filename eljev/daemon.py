"""Local HTTP daemon for el-jev."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import os
from pathlib import Path
import signal
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from typing import Any

from . import config
from .decisionlog import DecisionLog
from .engines.cohere import CohereClient, CohereError
from .engines.systemone import SystemOneEngine, SystemOneError
from .pregate import check_pregate
from .types import Candidate, DecisionRecord, SCHEMA
from .validate import InputValidationError, validate_request, validate_top_n
from .verdict import calibration_for, load_calibration, verdict_from_engine


VERSION = "0.2.0"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
# Token check and idle-connection probe are cheap and never billed; 30 s keeps a
# server-closed keep-alive from being the connection the next hook request uses.
WARM_INTERVAL_S = 30.0


def _elapsed(
    total_start: float,
    pregate_start: float,
    engine_start: float | None,
    engine_end: float | None,
) -> dict[str, float]:
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


def _write_pidfile(path: Path, pid: int, port: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent))
    record = {"pid": pid, "port": port, "started_at": time.time(), "exe": sys.executable}
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(record, stream)
            stream.write("\n")
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


def _pidfile_owner(content: str) -> int | None:
    try:
        value = json.loads(content)
    except ValueError:
        return None
    if isinstance(value, dict) and isinstance(value.get("pid"), int):
        return int(value["pid"])
    return value if isinstance(value, int) else None


def _remove_pidfile_if_owner(path: Path, pid: int) -> None:
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if _pidfile_owner(content) != pid:
        return
    try:
        path.unlink()
    except OSError:
        pass


class EljevHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = sys.platform != "win32"

    def __init__(self, server_address: tuple[str, int], handler_class: type[BaseHTTPRequestHandler]) -> None:
        if server_address[0] != DEFAULT_HOST:
            raise ValueError("el-jev must bind to 127.0.0.1 only")
        super().__init__(server_address, handler_class)
        self.started_at = time.monotonic()
        self.engine_gate = threading.BoundedSemaphore(1)
        self.cohere = CohereClient()
        self.systemone = SystemOneEngine(cohere=self.cohere)
        self.decision_log = DecisionLog()
        self.last_warm_ok = False
        self._warm_stop = threading.Event()

    def server_bind(self) -> None:
        if sys.platform == "win32":
            exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
            if exclusive is not None:
                self.socket.setsockopt(socket.SOL_SOCKET, int(exclusive), 1)
        super().server_bind()


class EljevRequestHandler(BaseHTTPRequestHandler):
    server_version = "eljev/0.2.0"
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

    def _request_allowed(self, *, require_json: bool) -> bool:
        if self.headers.get("Origin"):
            self._send_json(403, {"error_kind": "forbidden", "message": "Origin header is not allowed"})
            return False

        host = (self.headers.get("Host") or "").strip().lower()
        port = int(self.server.server_port)
        allowed_hosts = {
            "127.0.0.1",
            "localhost",
            f"127.0.0.1:{port}",
            f"localhost:{port}",
        }
        if host not in allowed_hosts:
            self._send_json(403, {"error_kind": "forbidden", "message": "Host header is not allowed"})
            return False

        if require_json:
            content_type = (self.headers.get("Content-Type") or "").strip().lower()
            if not content_type.startswith("application/json"):
                self._send_json(
                    415,
                    {
                        "error_kind": "invalid_input",
                        "message": "Content-Type must start with application/json",
                    },
                )
                return False
        return True

    def do_GET(self) -> None:
        if not self._request_allowed(require_json=False):
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/health":
            self._health()
            return
        if parsed.path == "/v1/log":
            values = parse_qs(parsed.query)
            try:
                limit = int(values.get("limit", ["100"])[0])
            except ValueError:
                limit = 100
            self._send_json(200, {"records": self.app.decision_log.read_last(max(1, min(limit, 1000)))})
            return
        self._send_json(404, {"error_kind": "http", "message": "not found"})

    def do_POST(self) -> None:
        if not self._request_allowed(require_json=True):
            return
        parsed = urlsplit(self.path)
        if parsed.path == "/v1/screen":
            self._screen()
            return
        if parsed.path == "/v1/decide":
            self._decide()
            return
        if parsed.path == "/v1/shutdown":
            self._shutdown()
            return
        self._send_json(404, {"error_kind": "http", "message": "not found"})

    def _health(self) -> None:
        policy = config.coverage_policy()
        calibration_version = "none"
        if policy == "calibrated":
            params, _ = calibration_for(load_calibration(), "screen")
            if params is not None:
                calibration_version = str(params["calibration_version"])
        passthrough = bool(os.environ.get("ELJEV_SYSTEMONE_URL", "").strip())
        systemone_mode = "passthrough" if passthrough else "cohere" if self.app.cohere.endpoint else "heuristic"
        self._send_json(
            200,
            {
                "status": "ok",
                "version": VERSION,
                "schema": SCHEMA,
                "pid": os.getpid(),
                "engines": {
                    "cohere": "ready" if self.app.cohere.endpoint else "absent",
                    "systemone": systemone_mode,
                },
                "calibration_version": calibration_version,
                "coverage_policy": policy,
                "auth": config.auth_mode(),
                "warm": bool(self.app.last_warm_ok),
                "uptime_s": round(time.monotonic() - self.app.started_at, 3),
            },
        )

    def _shutdown(self) -> None:
        pid = os.getpid()
        self._send_json(200, {"status": "stopping", "pid": pid})
        threading.Thread(target=self.app.shutdown, daemon=True).start()

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
        policy = config.coverage_policy()
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
                engine=config.cohere_deployment(),
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
                engine=config.cohere_deployment(),
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
            engine=config.cohere_deployment(),
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
        started = time.perf_counter()
        engine_started = started
        try:
            payload = self._read_json()
            if not isinstance(payload, Mapping):
                raise InputValidationError("request body must be an object")
            state = payload.get("state")
            question = payload.get("question")
            if isinstance(state, str):
                state_ok = bool(state.strip())
            else:
                state_ok = isinstance(state, (dict, list))
            if not state_ok:
                raise InputValidationError("state must be a non-empty string or JSON object/array")
            if not isinstance(question, Mapping):
                raise InputValidationError("question must be an object")
            kind = str(question.get("kind") or question.get("type") or "").strip().lower()
            if kind not in {"choice", "noul", "score"}:
                raise InputValidationError("question.kind must be choice, noul, or score")
            engine_started = time.perf_counter()
            with self.app.engine_gate:
                result = self.app.systemone.decide(state, question)
            engine_ended = time.perf_counter()
            self.app.decision_log.append(result)
            self._send_json(200, result)
            return
        except InputValidationError as exc:
            self._send_json(
                400,
                {"error_kind": "invalid_input", "message": str(exc), "exit_code": 1},
            )
            return
        except SystemOneError as exc:
            engine_ended = time.perf_counter()
            if exc.error_kind == "invalid_input":
                self._send_json(
                    400,
                    {"error_kind": "invalid_input", "message": str(exc), "exit_code": 1},
                )
                return
            question = payload.get("question") if isinstance(payload, Mapping) else {}
            question_id = str(question.get("id") or "q1") if isinstance(question, Mapping) else "q1"
            kind = (
                str(question.get("kind") or question.get("type") or "choice")
                if isinstance(question, Mapping)
                else "choice"
            )
            record = DecisionRecord(
                shape="decide",
                criterion=str(question.get("instructions") or question.get("description") or "")
                if isinstance(question, Mapping)
                else "",
                n_candidates=0,
                tier_path=["systemone", "cohere"],
                engine=config.cohere_deployment()
                if self.app.cohere.endpoint
                else "local_heuristic",
                choice=None,
                choice_index=None,
                status="engine_error",
                exit_code=2,
                calibration_version="none",
                coverage_policy=config.coverage_policy(),
                elapsed_ms=_elapsed(started, started, engine_started, engine_ended),
                error_kind=exc.error_kind,
                notes=[str(exc)],
            )
            body = record.to_dict()
            body.update(
                {
                    "question_id": question_id,
                    "kind": kind,
                    "confidence": None,
                    "probabilities": None,
                    "margin": None,
                    "noul": None,
                    "score": None,
                }
            )
            self.app.decision_log.append(body)
            self._send_json(200, body)
            return
        except Exception:
            self._send_json(500, {"error_kind": "internal", "message": "internal server error"})


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


def _run_warm_loop(server: EljevHTTPServer) -> None:
    while not server._warm_stop.is_set():
        try:
            server.last_warm_ok = bool(server.cohere.warm())
        except Exception:
            server.last_warm_ok = False
        server._warm_stop.wait(WARM_INTERVAL_S)


def run(host: str | None = None, port: int | None = None) -> int:
    try:
        server = create_server(host, port)
    except OSError:
        return 3

    pid = os.getpid()
    pid_path = config.pidfile_path()
    _write_pidfile(pid_path, pid, int(server.server_port))

    def shutdown_from_signal(signum: int, frame: Any) -> None:
        del signum, frame
        threading.Thread(target=server.shutdown, daemon=True).start()

    try:
        for signal_name in ("SIGTERM", "SIGBREAK"):
            sig = getattr(signal, signal_name, None)
            if sig is None:
                continue
            try:
                signal.signal(sig, shutdown_from_signal)
            except Exception:
                continue

        warm_thread = threading.Thread(target=_run_warm_loop, args=(server,), daemon=True)
        warm_thread.start()
        server.serve_forever()
        return 0
    finally:
        server._warm_stop.set()
        server.cohere.close()
        server.server_close()
        _remove_pidfile_if_owner(pid_path, pid)
