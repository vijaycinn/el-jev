"""Fast local client for the resident el-jev daemon.

The module intentionally uses a tiny hand-rolled HTTP/1.1 implementation.
Importing the client must not pull in ``http.client``, ``email``, or
``argparse`` because this module is also used by latency-sensitive callers.
"""

from __future__ import annotations

import json
import os
import socket


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_TIMEOUT_MS = 2500
START_DAEMON_NOTE = "Start the daemon with: eljev daemon start"


class ClientError(RuntimeError):
    """Base class for client-side transport and response failures."""

    def __init__(self, error_kind: str, message: str) -> None:
        super().__init__(message)
        self.error_kind = error_kind


class TransportError(ClientError):
    """The daemon could not be reached or the socket failed."""


class ResponseError(ClientError):
    """The daemon returned an unusable HTTP response."""


def _timeout_seconds(value: float | int | None = None) -> float:
    if value is not None:
        return max(0.001, float(value))
    raw = os.environ.get("ELJEV_TIMEOUT_MS", str(DEFAULT_TIMEOUT_MS))
    try:
        milliseconds = int(raw)
    except (TypeError, ValueError):
        milliseconds = DEFAULT_TIMEOUT_MS
    return max(0.001, milliseconds / 1000.0)


def _port(value: int | str | None = None) -> int:
    raw = value if value is not None else os.environ.get("ELJEV_PORT", str(DEFAULT_PORT))
    try:
        port = int(raw)
    except (TypeError, ValueError) as exc:
        raise TransportError("connection", "daemon port is invalid") from exc
    if not 1 <= port <= 65535:
        raise TransportError("connection", "daemon port is invalid")
    return port


def _decode_response(raw: bytes) -> tuple[int, bytes]:
    separator = raw.find(b"\r\n\r\n")
    if separator < 0:
        raise ResponseError("malformed", "daemon returned an incomplete HTTP response")

    header_bytes = raw[:separator]
    body = raw[separator + 4 :]
    lines = header_bytes.split(b"\r\n")
    if not lines:
        raise ResponseError("malformed", "daemon returned an empty HTTP response")
    try:
        status_parts = lines[0].decode("ascii").split(" ", 2)
        status = int(status_parts[1])
    except (IndexError, UnicodeDecodeError, ValueError) as exc:
        raise ResponseError("malformed", "daemon returned an invalid HTTP status") from exc

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        try:
            name, value = line.decode("ascii").split(":", 1)
        except (UnicodeDecodeError, ValueError) as exc:
            raise ResponseError("malformed", "daemon returned an invalid HTTP header") from exc
        headers[name.strip().lower()] = value.strip()

    if headers.get("transfer-encoding", "").lower() == "chunked":
        raise ResponseError("malformed", "chunked daemon responses are not supported")

    if "content-length" in headers:
        try:
            length = int(headers["content-length"])
        except ValueError as exc:
            raise ResponseError("malformed", "daemon returned an invalid content length") from exc
        if length < 0 or len(body) < length:
            raise ResponseError("malformed", "daemon returned a truncated body")
        body = body[:length]
    return status, body


def _request(
    host: str,
    port: int,
    method: str,
    path: str,
    payload: object | None,
    timeout_s: float,
) -> tuple[int, bytes]:
    body = b"" if payload is None else json.dumps(
        payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")
    ).encode("utf-8")
    request = (
        f"{method} {path} HTTP/1.1\r\n"
        f"Host: {DEFAULT_HOST}:{port}\r\n"
        "Accept: application/json\r\n"
        "Connection: close\r\n"
        + ("Content-Type: application/json\r\n" if payload is not None else "")
        + f"Content-Length: {len(body)}\r\n"
        "\r\n"
    ).encode("ascii") + body

    try:
        with socket.create_connection((host, port), timeout=timeout_s) as connection:
            connection.settimeout(timeout_s)
            connection.sendall(request)
            response = bytearray()
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                response.extend(chunk)
                if len(response) > 8_000_000:
                    raise ResponseError("malformed", "daemon response is too large")
    except ResponseError:
        raise
    except socket.timeout as exc:
        raise TransportError("timeout", "daemon request timed out") from exc
    except socket.gaierror as exc:
        raise TransportError("dns", "daemon hostname could not be resolved") from exc
    except (ConnectionRefusedError, ConnectionResetError, BrokenPipeError) as exc:
        raise TransportError("daemon_unavailable", START_DAEMON_NOTE) from exc
    except OSError as exc:
        raise TransportError("connection", "daemon connection failed") from exc

    return _decode_response(bytes(response))


def _request_json(
    host: str,
    port: int,
    method: str,
    path: str,
    payload: object | None,
    timeout_s: float,
) -> tuple[int, object]:
    status, raw_body = _request(host, port, method, path, payload, timeout_s)
    try:
        decoded = json.loads(raw_body)
    except (TypeError, ValueError) as exc:
        raise ResponseError("malformed", "daemon returned invalid JSON") from exc
    return status, decoded


def _error_record(
    *,
    shape: str,
    criterion: str = "",
    n_candidates: int = 0,
    error_kind: str,
    status: str = "engine_error",
    note: str,
    tier_path: list[str] | None = None,
) -> dict[str, object]:
    from .types import DecisionRecord

    return DecisionRecord(
        shape=shape,
        criterion=criterion,
        n_candidates=n_candidates,
        tier_path=tier_path or [],
        status=status,
        exit_code=2,
        error_kind=error_kind,
        notes=[note],
    ).to_dict()


def _response_error_record(
    *,
    shape: str,
    criterion: str = "",
    n_candidates: int = 0,
    status: int,
    body: object,
) -> dict[str, object]:
    error_kind = "http"
    note = f"daemon returned HTTP {status}"
    if isinstance(body, dict):
        candidate_kind = body.get("error_kind")
        if isinstance(candidate_kind, str) and candidate_kind:
            error_kind = candidate_kind
        message = body.get("message")
        if isinstance(message, str) and message:
            note = message
    return _error_record(
        shape=shape,
        criterion=criterion,
        n_candidates=n_candidates,
        error_kind=error_kind,
        note=note,
    )


def _validate_decide(state: object, question: object) -> tuple[str, dict[str, object]]:
    if not isinstance(state, (str, dict, list)) or (isinstance(state, str) and not state.strip()):
        from .validate import InputValidationError

        raise InputValidationError("state must be a non-empty string or JSON object/list", field="state")
    if not isinstance(question, dict):
        from .validate import InputValidationError

        raise InputValidationError("question must be an object", field="question")
    question_id = question.get("id")
    question_kind = (question.get("kind") or question.get("type") or "").lower()
    if not isinstance(question_id, str) or not question_id.strip():
        from .validate import InputValidationError

        raise InputValidationError("question.id must be a non-empty string", field="question")
    if question_kind not in {"choice", "noul", "score"}:
        from .validate import InputValidationError

        raise InputValidationError("question.kind must be choice, noul, or score", field="question")
    if question_kind == "choice":
        options = question.get("options")
        criteria = question.get("criteria")
        if isinstance(options, list):
            if not options:
                from .validate import InputValidationError

                raise InputValidationError("choice questions must provide at least one option", field="question")
        elif isinstance(criteria, dict):
            if not criteria:
                from .validate import InputValidationError

                raise InputValidationError("choice criteria mapping must not be empty", field="question")
        else:
            from .validate import InputValidationError

            raise InputValidationError("choice questions must contain an options list or criteria mapping", field="question")
    if question_kind == "score":
        criteria = question.get("criteria")
        if not isinstance(criteria, list):
            from .validate import InputValidationError

            raise InputValidationError("score questions must contain a criteria list", field="question")
        if not 2 <= len(criteria) <= 10:
            from .validate import InputValidationError

            raise InputValidationError("score criteria list must contain 2-10 items", field="question")
    return state, question


class EljevClient:
    """Small client for the loopback daemon."""

    def __init__(
        self,
        host: str | None = None,
        port: int | str | None = None,
        timeout_s: float | None = None,
    ) -> None:
        self.host = host or os.environ.get("ELJEV_HOST", DEFAULT_HOST)
        self.port = port
        self.timeout_s = timeout_s

    def _call(
        self,
        method: str,
        path: str,
        payload: object | None = None,
    ) -> tuple[int, object]:
        if self.host != DEFAULT_HOST:
            raise TransportError("connection", "el-jev client only supports 127.0.0.1")
        return _request_json(
            self.host,
            _port(self.port),
            method,
            path,
            payload,
            _timeout_seconds(self.timeout_s),
        )

    def screen(
        self,
        criterion: object,
        candidates: object,
        *,
        top_n: object | None = None,
        tier: str = "auto",
    ) -> dict[str, object]:
        from .validate import validate_request, validate_top_n

        validated = validate_request(criterion, candidates)
        selected_top_n = validate_top_n(top_n, len(validated.candidates))
        if tier not in {"auto", "cohere", "local", "pregate_only"}:
            from .validate import InputValidationError

            raise InputValidationError(
                "tier must be auto, cohere, local, or pregate_only",
                field="tier",
            )
        request = {
            "criterion": validated.criterion,
            "candidates": [candidate.to_dict() for candidate in validated.candidates],
            "top_n": selected_top_n,
            "tier": tier,
        }
        try:
            status, body = self._call("POST", "/v1/screen", request)
        except TransportError as exc:
            return _error_record(
                shape="screen",
                criterion=validated.criterion,
                n_candidates=len(validated.candidates),
                error_kind=exc.error_kind,
                note=START_DAEMON_NOTE if exc.error_kind == "daemon_unavailable" else str(exc),
                tier_path=[],
            )
        except ResponseError as exc:
            return _error_record(
                shape="screen",
                criterion=validated.criterion,
                n_candidates=len(validated.candidates),
                error_kind=exc.error_kind,
                status="invalid_response",
                note=str(exc),
                tier_path=[],
            )
        if status != 200:
            return _response_error_record(
                shape="screen",
                criterion=validated.criterion,
                n_candidates=len(validated.candidates),
                status=status,
                body=body,
            )
        if not isinstance(body, dict):
            return _error_record(
                shape="screen",
                criterion=validated.criterion,
                n_candidates=len(validated.candidates),
                error_kind="malformed",
                status="invalid_response",
                note="daemon returned a non-object decision record",
                tier_path=[],
            )
        return body

    def decide(self, state: object, question: object) -> dict[str, object]:
        normalized_state, normalized_question = _validate_decide(state, question)
        try:
            status, body = self._call(
                "POST",
                "/v1/decide",
                {"state": normalized_state, "question": normalized_question},
            )
        except TransportError as exc:
            return _error_record(
                shape="decide",
                error_kind=exc.error_kind,
                note=START_DAEMON_NOTE if exc.error_kind == "daemon_unavailable" else str(exc),
            )
        except ResponseError as exc:
            return _error_record(
                shape="decide",
                error_kind=exc.error_kind,
                status="invalid_response",
                note=str(exc),
            )
        if status != 200:
            return _response_error_record(shape="decide", status=status, body=body)
        if not isinstance(body, dict):
            return _error_record(
                shape="decide",
                error_kind="malformed",
                status="invalid_response",
                note="daemon returned a non-object decision record",
            )
        return body

    def health(self) -> dict[str, object]:
        try:
            status, body = self._call("GET", "/health")
        except TransportError as exc:
            return {
                "status": "down",
                "error_kind": exc.error_kind,
                "notes": [
                    START_DAEMON_NOTE
                    if exc.error_kind == "daemon_unavailable"
                    else str(exc)
                ],
            }
        except ResponseError as exc:
            return {"status": "down", "error_kind": exc.error_kind, "notes": [str(exc)]}
        if status != 200:
            return {"status": "down", "error_kind": "http", "notes": [f"daemon returned HTTP {status}"]}
        if not isinstance(body, dict):
            return {"status": "down", "error_kind": "malformed", "notes": ["health response was not an object"]}
        return body

    def log(self, limit: int = 10) -> dict[str, object]:
        try:
            status, body = self._call("GET", f"/v1/log?limit={limit}")
            if isinstance(body, dict):
                return body
            return {"records": []}
        except Exception as exc:
            return {"error": str(exc), "records": []}

    def shutdown(self) -> dict[str, object]:
        try:
            status, body = self._call("POST", "/v1/shutdown", {})
        except TransportError as exc:
            return {
                "status": "down",
                "error_kind": exc.error_kind,
                "notes": [
                    START_DAEMON_NOTE
                    if exc.error_kind == "daemon_unavailable"
                    else str(exc)
                ],
            }
        except ResponseError as exc:
            return {"status": "down", "error_kind": exc.error_kind, "notes": [str(exc)]}
        if status != 200:
            return {
                "status": "down",
                "error_kind": "http",
                "notes": [f"daemon returned HTTP {status}"],
            }
        if not isinstance(body, dict):
            return {"status": "down", "error_kind": "malformed", "notes": ["shutdown response was not an object"]}
        return body


Client = EljevClient


def screen(
    criterion: object,
    candidates: object,
    *,
    top_n: object | None = None,
    tier: str = "auto",
) -> dict[str, object]:
    return EljevClient().screen(criterion, candidates, top_n=top_n, tier=tier)


def decide(state: object, question: object) -> dict[str, object]:
    return EljevClient().decide(state, question)


def health() -> dict[str, object]:
    return EljevClient().health()


__all__ = [
    "Client",
    "ClientError",
    "EljevClient",
    "ResponseError",
    "TransportError",
    "decide",
    "health",
    "screen",
]
