"""MCP stdio proxy for the local el-jev daemon."""

from __future__ import annotations

import json
import os
import socket
import ssl
import sys
import uuid
from datetime import datetime, timezone
from http import HTTPStatus
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SCHEMA = "eljev.decision/1"
SERVER_NAME = "eljev"
SERVER_VERSION = "0.1.0"
DEFAULT_PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = {
    "2024-11-05",
    "2025-03-26",
    "2025-06-18",
}
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_TIMEOUT_MS = 2500
DEFAULT_COVERAGE_POLICY = "always_abstain_v0"
MAX_CANDIDATES = 250
MAX_CANDIDATE_CHARS = 2000
MAX_TOTAL_CANDIDATE_CHARS = 100_000


class InputValidationError(ValueError):
    """Raised when a tool argument violates the contract."""


class DaemonError(Exception):
    """A transport or response error from the local daemon."""

    def __init__(self, error_kind: str, message: str) -> None:
        super().__init__(message)
        self.error_kind = error_kind
        self.message = message


class DaemonHTTPError(DaemonError):
    """An HTTP error with a structured daemon response, when available."""

    def __init__(
        self,
        error_kind: str,
        message: str,
        status_code: int,
        payload: dict[str, Any] | None,
    ) -> None:
        super().__init__(error_kind, message)
        self.status_code = status_code
        self.payload = payload


def _now_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _coverage_policy() -> str:
    return os.environ.get("ELJEV_COVERAGE_POLICY", DEFAULT_COVERAGE_POLICY)


def _timeout_seconds() -> float:
    raw = os.environ.get("ELJEV_TIMEOUT_MS", str(DEFAULT_TIMEOUT_MS))
    try:
        timeout_ms = int(raw)
    except ValueError:
        timeout_ms = DEFAULT_TIMEOUT_MS
    return max(timeout_ms, 1) / 1000.0


def _daemon_base_url() -> str:
    host = os.environ.get("ELJEV_HOST", DEFAULT_HOST)
    port = os.environ.get("ELJEV_PORT", str(DEFAULT_PORT))
    return f"http://{host}:{port}"


def _json_response(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)


def _write_message(value: dict[str, Any]) -> None:
    sys.stdout.write(_json_response(value) + "\n")
    sys.stdout.flush()


def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


def _error_decision(
    shape: str,
    criterion: str,
    n_candidates: int,
    error_kind: str,
    note: str,
    *,
    status: str | None = None,
) -> dict[str, Any]:
    if status is None:
        status = "invalid_response" if error_kind == "malformed" else "engine_error"
    return {
        "schema": SCHEMA,
        "decision_id": str(uuid.uuid4()),
        "ts": _now_iso(),
        "shape": shape,
        "criterion": criterion,
        "n_candidates": n_candidates,
        "tier_path": [],
        "engine": None,
        "engine_version": "1",
        "choice": None,
        "choice_index": None,
        "status": status,
        "exit_code": 2,
        "raw_top_score": None,
        "raw_runner_up": None,
        "margin_raw": None,
        "calibrated_probability": None,
        "margin_calibrated": None,
        "calibration_version": "none",
        "coverage_policy": _coverage_policy(),
        "results": [],
        "elapsed_ms": {"total": 0.0, "pregate": 0.0, "engine": 0.0, "overhead": 0.0},
        "escalation_reason": None,
        "error_kind": error_kind,
        "notes": [note],
    }


def _health_unavailable(error: DaemonError) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "status": "degraded",
        "version": None,
        "engines": {},
        "calibration_version": "none",
        "coverage_policy": _coverage_policy(),
        "uptime_s": None,
        "error_kind": error.error_kind,
        "instructions": "Run `eljev daemon start` and retry.",
        "notes": [error.message],
    }


def _tool_result(payload: dict[str, Any]) -> dict[str, Any]:
    payload.setdefault("calibration_version", "none")
    payload.setdefault("coverage_policy", _coverage_policy())
    has_error = payload.get("error_kind") is not None
    return {
        "content": [{"type": "text", "text": _json_response(payload)}],
        "structuredContent": payload,
        "isError": has_error,
    }


def _read_json_body(raw: bytes) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _classify_transport_error(error: BaseException) -> str:
    if isinstance(error, socket.timeout | TimeoutError):
        return "timeout"
    if isinstance(error, ssl.SSLError):
        return "tls"
    if isinstance(error, socket.gaierror):
        return "dns"
    if isinstance(error, ConnectionRefusedError):
        return "daemon_unavailable"
    if isinstance(error, ConnectionResetError):
        return "connection"
    return "connection"


def _request_json(method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url = _daemon_base_url() + path
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = _json_response(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)

    try:
        with urlopen(request, timeout=_timeout_seconds()) as response:
            raw = response.read()
            parsed = _read_json_body(raw)
            if parsed is None:
                raise DaemonError("malformed", "The daemon returned a non-JSON response.")
            return parsed
    except HTTPError as error:
        raw = error.read()
        parsed = _read_json_body(raw)
        if parsed is not None:
            error_kind = "rate_limited" if error.code == HTTPStatus.TOO_MANY_REQUESTS else "http"
            if error.code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
                error_kind = "auth"
            raise DaemonHTTPError(
                error_kind,
                f"The daemon returned HTTP {error.code}.",
                error.code,
                parsed,
            ) from error
        if error.code in (HTTPStatus.UNAUTHORIZED, HTTPStatus.FORBIDDEN):
            error_kind = "auth"
        elif error.code == HTTPStatus.TOO_MANY_REQUESTS:
            error_kind = "rate_limited"
        else:
            error_kind = "http"
        raise DaemonError(error_kind, f"The daemon returned HTTP {error.code}.") from error
    except URLError as error:
        reason = error.reason
        if isinstance(reason, BaseException):
            error_kind = _classify_transport_error(reason)
            message = str(reason) or "The daemon request failed."
        else:
            error_kind = "connection"
            message = str(reason) or "The daemon request failed."
        if error_kind == "connection" and "refused" in message.lower():
            error_kind = "daemon_unavailable"
        raise DaemonError(error_kind, message) from error
    except (socket.timeout, TimeoutError, ssl.SSLError, ConnectionRefusedError, ConnectionResetError) as error:
        raise DaemonError(_classify_transport_error(error), str(error) or "The daemon request failed.") from error
    except OSError as error:
        error_kind = _classify_transport_error(error)
        if "refused" in str(error).lower():
            error_kind = "daemon_unavailable"
        raise DaemonError(error_kind, str(error) or "The daemon request failed.") from error


def _require_object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise InputValidationError(f"{label} must be an object.")
    return value


def _reject_unknown_keys(value: dict[str, Any], allowed: Iterable[str], label: str) -> None:
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise InputValidationError(f"{label} contains unsupported field(s): {', '.join(unknown)}.")


def _require_nonempty_string(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InputValidationError(f"{label} must be a non-empty string.")
    return value


def _validate_screen_arguments(arguments: Any) -> dict[str, Any]:
    args = _require_object(arguments, "arguments")
    _reject_unknown_keys(args, {"criterion", "candidates", "top_n", "tier"}, "arguments")
    criterion = _require_nonempty_string(args.get("criterion"), "criterion")

    candidates = args.get("candidates")
    if not isinstance(candidates, list):
        raise InputValidationError("candidates must be an array.")
    if not 1 <= len(candidates) <= MAX_CANDIDATES:
        raise InputValidationError("candidates must contain 1-250 items.")

    normalized: list[dict[str, str]] = []
    ids: set[str] = set()
    total_chars = 0
    for position, candidate in enumerate(candidates):
        item = _require_object(candidate, f"candidates[{position}]")
        _reject_unknown_keys(item, {"id", "text"}, f"candidates[{position}]")
        candidate_id = _require_nonempty_string(item.get("id"), f"candidates[{position}].id")
        text = _require_nonempty_string(item.get("text"), f"candidates[{position}].text")
        if len(text) > MAX_CANDIDATE_CHARS:
            raise InputValidationError(
                f"candidates[{position}].text must be at most {MAX_CANDIDATE_CHARS} characters."
            )
        if candidate_id in ids:
            raise InputValidationError(f"duplicate candidate id: {candidate_id}")
        ids.add(candidate_id)
        total_chars += len(text)
        normalized.append({"id": candidate_id, "text": text})
    if total_chars > MAX_TOTAL_CANDIDATE_CHARS:
        raise InputValidationError(
            f"total candidate text must be at most {MAX_TOTAL_CANDIDATE_CHARS} characters."
        )

    top_n = args.get("top_n", min(10, len(normalized)))
    if isinstance(top_n, bool) or not isinstance(top_n, int) or not 1 <= top_n <= MAX_CANDIDATES:
        raise InputValidationError("top_n must be an integer from 1-250.")
    tier = args.get("tier", "auto")
    if tier not in {"auto", "cohere", "local", "pregate_only"}:
        raise InputValidationError("tier must be one of auto, cohere, local, pregate_only.")

    return {
        "criterion": criterion,
        "candidates": normalized,
        "top_n": top_n,
        "tier": tier,
    }


def _validate_decide_arguments(arguments: Any) -> dict[str, Any]:
    args = _require_object(arguments, "arguments")
    _reject_unknown_keys(args, {"state", "question"}, "arguments")
    state = _require_nonempty_string(args.get("state"), "state")
    question = _require_object(args.get("question"), "question")
    _reject_unknown_keys(question, {"id", "kind", "options"}, "question")
    question_id = _require_nonempty_string(question.get("id"), "question.id")
    kind = question.get("kind")
    if kind not in {"choice", "noul"}:
        raise InputValidationError("question.kind must be one of choice or noul.")

    options = question.get("options", [])
    if not isinstance(options, list):
        raise InputValidationError("question.options must be an array.")
    option_ids: set[str] = set()
    normalized_options: list[dict[str, str]] = []
    for position, option in enumerate(options):
        item = _require_object(option, f"question.options[{position}]")
        _reject_unknown_keys(item, {"id", "description"}, f"question.options[{position}]")
        option_id = _require_nonempty_string(item.get("id"), f"question.options[{position}].id")
        description = _require_nonempty_string(
            item.get("description"), f"question.options[{position}].description"
        )
        if option_id in option_ids:
            raise InputValidationError(f"duplicate option id: {option_id}")
        option_ids.add(option_id)
        normalized_options.append({"id": option_id, "description": description})
    if kind == "choice" and not normalized_options:
        raise InputValidationError("choice questions require at least one option.")

    return {
        "state": state,
        "question": {"id": question_id, "kind": kind, "options": normalized_options},
    }


def _proxy_screen(arguments: Any) -> dict[str, Any]:
    request = _validate_screen_arguments(arguments)
    try:
        payload = _request_json("POST", "/v1/screen", request)
    except DaemonHTTPError as error:
        if error.payload is not None:
            return error.payload
        return _error_decision(
            "screen",
            request["criterion"],
            len(request["candidates"]),
            error.error_kind,
            error.message,
        )
    except DaemonError as error:
        return _error_decision(
            "screen",
            request["criterion"],
            len(request["candidates"]),
            error.error_kind,
            "Run `eljev daemon start` and retry." if error.error_kind == "daemon_unavailable" else error.message,
        )
    return payload


def _proxy_decide(arguments: Any) -> dict[str, Any]:
    request = _validate_decide_arguments(arguments)
    try:
        payload = _request_json("POST", "/v1/decide", request)
    except DaemonHTTPError as error:
        if error.payload is not None:
            return error.payload
        return _error_decision(
            "decide",
            request["question"]["id"],
            0,
            error.error_kind,
            error.message,
        )
    except DaemonError as error:
        return _error_decision(
            "decide",
            request["question"]["id"],
            0,
            error.error_kind,
            "Run `eljev daemon start` and retry." if error.error_kind == "daemon_unavailable" else error.message,
        )
    return payload


def _proxy_health() -> dict[str, Any]:
    try:
        return _request_json("GET", "/health")
    except DaemonError as error:
        return _health_unavailable(error)


def _screen_schema() -> dict[str, Any]:
    nonempty_text = {"type": "string", "minLength": 1, "maxLength": MAX_CANDIDATE_CHARS, "pattern": r"[\s\S]*\S[\s\S]*"}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["criterion", "candidates"],
        "properties": {
            "criterion": {"type": "string", "minLength": 1, "pattern": r"[\s\S]*\S[\s\S]*"},
            "candidates": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_CANDIDATES,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["id", "text"],
                    "properties": {
                        "id": {"type": "string", "minLength": 1, "pattern": r"[\s\S]*\S[\s\S]*"},
                        "text": nonempty_text,
                    },
                },
            },
            "top_n": {"type": "integer", "minimum": 1, "maximum": MAX_CANDIDATES},
            "tier": {"type": "string", "enum": ["auto", "cohere", "local", "pregate_only"]},
        },
        "x-eljev-constraints": {
            "unique_candidate_ids": True,
            "max_total_candidate_chars": MAX_TOTAL_CANDIDATE_CHARS,
        },
    }


def _decide_schema() -> dict[str, Any]:
    option_schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["id", "description"],
        "properties": {
            "id": {"type": "string", "minLength": 1, "pattern": r"[\s\S]*\S[\s\S]*"},
            "description": {"type": "string", "minLength": 1, "pattern": r"[\s\S]*\S[\s\S]*"},
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["state", "question"],
        "properties": {
            "state": {"type": "string", "minLength": 1, "pattern": r"[\s\S]*\S[\s\S]*"},
            "question": {
                "type": "object",
                "additionalProperties": False,
                "required": ["id", "kind"],
                "properties": {
                    "id": {"type": "string", "minLength": 1, "pattern": r"[\s\S]*\S[\s\S]*"},
                    "kind": {"type": "string", "enum": ["choice", "noul"]},
                    "options": {"type": "array", "items": option_schema},
                },
                "allOf": [
                    {
                        "if": {"properties": {"kind": {"const": "choice"}}},
                        "then": {"required": ["options"], "properties": {"options": {"minItems": 1}}},
                    }
                ],
            },
        },
    }


TOOLS = [
    {
        "name": "eljev_screen",
        "description": (
            "Submit one criterion and capped candidate records to the local el-jev daemon. "
            "Returns a typed, auditable ranking. v0 uses always_abstain_v0, so a non-trivial "
            "ranking is needs_review with exit_code 2, never approval."
        ),
        "inputSchema": _screen_schema(),
    },
    {
        "name": "eljev_decide",
        "description": (
            "Submit state and a typed choice or noul question to the local el-jev daemon. "
            "Returns the daemon's auditable decision record or explicit backend status. "
            "v0 does not auto-approve non-trivial decisions."
        ),
        "inputSchema": _decide_schema(),
    },
    {
        "name": "eljev_health",
        "description": (
            "Report local el-jev daemon status, engines, calibration version, and coverage "
            "policy. If the daemon is absent, returns structured daemon_unavailable guidance."
        ),
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {},
        },
    },
]


def _initialize_result(params: Any) -> dict[str, Any]:
    requested = params.get("protocolVersion") if isinstance(params, dict) else None
    protocol_version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
    return {
        "protocolVersion": protocol_version,
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        "instructions": (
            "el-jev is a typed, auditable, capped decision proxy. "
            "The local daemon must be started separately."
        ),
    }


def _handle_request(message: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
    request_id = message.get("id")
    method = message.get("method")
    params = message.get("params", {})

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": request_id, "result": _initialize_result(params)}, False
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}, False
    if method == "tools/call":
        params_object = _require_object(params, "params")
        _reject_unknown_keys(params_object, {"name", "arguments"}, "params")
        name = params_object.get("name")
        if name not in {"eljev_screen", "eljev_decide", "eljev_health"}:
            raise InputValidationError("params.name must name an exposed tool.")
        arguments = params_object.get("arguments", {})
        if name == "eljev_screen":
            payload = _proxy_screen(arguments)
        elif name == "eljev_decide":
            payload = _proxy_decide(arguments)
        else:
            _require_object(arguments, "arguments")
            if arguments:
                raise InputValidationError("eljev_health takes no arguments.")
            payload = _proxy_health()
        return {"jsonrpc": "2.0", "id": request_id, "result": _tool_result(payload)}, False
    if method == "shutdown":
        return {"jsonrpc": "2.0", "id": request_id, "result": None}, False
    if method in {"notifications/initialized", "notifications/cancelled"}:
        return None, False
    if method == "exit":
        return None, True
    raise KeyError(method)


def _is_notification(message: dict[str, Any]) -> bool:
    return "id" not in message


def serve() -> None:
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            _write_message(_jsonrpc_error(None, -32700, "Parse error."))
            continue
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or not isinstance(message.get("method"), str):
            _write_message(_jsonrpc_error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request."))
            continue
        if _is_notification(message):
            try:
                _, should_exit = _handle_request(message)
            except (InputValidationError, KeyError):
                should_exit = message.get("method") == "exit"
            if should_exit:
                return
            continue
        try:
            response, should_exit = _handle_request(message)
        except InputValidationError as error:
            response = _jsonrpc_error(message.get("id"), -32602, str(error))
            should_exit = False
        except KeyError:
            response = _jsonrpc_error(message.get("id"), -32601, "Method not found.")
            should_exit = False
        except (TypeError, ValueError, OSError) as error:
            response = _jsonrpc_error(message.get("id"), -32603, f"Internal error: {error}")
            should_exit = False
        if response is not None:
            _write_message(response)
        if should_exit:
            return


def main() -> int:
    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
