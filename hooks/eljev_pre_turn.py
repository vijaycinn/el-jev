"""Fast, fail-open el-jev pre-turn hook."""

from __future__ import annotations

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

try:
    from eljev import config as eljev_config
except Exception:
    eljev_config = None


HOOK_SCHEMA = "eljev.hook/1"
DECISION_SCHEMA = "eljev.decision/1"
DEFAULT_TIMEOUT_MS = 750
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_MIN_WORDS = 4
MAX_PROMPT_CHARS = 4000
MAX_RESPONSE_BYTES = 1_048_576
MARKER_START = "<!--eljev.request:"
MARKER_END = "-->"
SPAWN_DEBOUNCE_SECONDS = 15.0
SPAWN_STAMP_NAME = "spawn.stamp"

INTENT_CRITERIA = {
    "code_modification": "Implement, write, edit, generate, refactor, or fix code in files",
    "review_audit": "Perform code review, security review, PR critique, or safety audit",
    "investigation_search": "Search codebase, find symbols/files, explore architecture, check logs or documentation",
    "execution_testing": "Run terminal commands, builds, test suites, or deployment tasks",
    "advisory_explanation": "Provide high-level architecture advice, conceptual explanations, or technical explanations",
}


class _NoRequest(Exception):
    """The payload intentionally does not require el-jev work."""


class _DaemonUnavailable(Exception):
    """The daemon is down or refused the connection."""


def _emit(value: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False) + "\n")
    sys.stdout.flush()


def _read_payload() -> dict[str, Any] | None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _timeout_ms() -> int:
    if eljev_config is not None:
        return eljev_config.hook_timeout_ms()
    raw = os.environ.get("ELJEV_HOOK_TIMEOUT_MS", str(DEFAULT_TIMEOUT_MS))
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_MS
    return max(parsed, 1)


def _min_words() -> int:
    raw = os.environ.get("ELJEV_HOOK_MIN_WORDS", str(DEFAULT_MIN_WORDS))
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_MIN_WORDS
    return max(parsed, 1)


def _word_count(value: str) -> int:
    return len([token for token in value.strip().split() if token])


def _remaining_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("hook deadline exceeded")
    return remaining


def _log(event: str, **fields: object) -> None:
    if eljev_config is None or not eljev_config.logging_enabled():
        return
    try:
        directory = eljev_config.log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        payload = {"ts_ms": int(time.time() * 1000), "event": event, **fields}
        with (directory / "hook.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(payload, ensure_ascii=True, separators=(",", ":")) + "\n")
    except OSError:
        return


def _native_marker_request(payload: dict[str, Any]) -> tuple[dict[str, Any], str] | None:
    prompt = payload.get("transformedPrompt")
    if not isinstance(prompt, str):
        prompt = payload.get("transformed_prompt")
    if not isinstance(prompt, str):
        return None
    start = prompt.find(MARKER_START)
    if start < 0:
        return None
    body_start = start + len(MARKER_START)
    end = prompt.find(MARKER_END, body_start)
    if end < 0:
        return None
    try:
        request = json.loads(prompt[body_start:end].strip())
    except json.JSONDecodeError:
        return None
    if not isinstance(request, dict):
        return None
    cleaned_prompt = (prompt[:start] + prompt[end + len(MARKER_END) :]).strip()
    return request, cleaned_prompt


def _generic_request(payload: dict[str, Any]) -> dict[str, Any] | None:
    if payload.get("schema") == HOOK_SCHEMA:
        return payload
    if isinstance(payload.get("criterion"), str) and "candidates" in payload:
        return payload
    return None


def _intent_request(prompt: str, payload: dict[str, Any]) -> tuple[dict[str, Any], str]:
    cleaned = prompt.strip()
    if not cleaned or len(cleaned) > MAX_PROMPT_CHARS:
        raise _NoRequest()
    if _word_count(cleaned) < _min_words():
        raise _NoRequest()
    request = {
        "path": "/v1/decide",
        "state": cleaned,
        "question": {
            "id": "intent_choice",
            "kind": "choice",
            "criteria": INTENT_CRITERIA,
        },
        "session_id": payload.get("sessionId") or payload.get("session_id"),
    }
    return request, cleaned


def _request_from_payload(payload: dict[str, Any], mode: str) -> tuple[dict[str, Any], bool, str | None]:
    generic = _generic_request(payload)
    if generic is not None:
        return generic, False, None

    marked = _native_marker_request(payload)
    if marked is not None:
        request, cleaned_prompt = marked
        request["session_id"] = payload.get("sessionId") or payload.get("session_id")
        return request, True, cleaned_prompt

    if mode == "marker":
        raise _NoRequest()

    prompt = payload.get("transformedPrompt")
    if not isinstance(prompt, str):
        prompt = payload.get("prompt")
    if not isinstance(prompt, str):
        raise _NoRequest()

    request, cleaned_prompt = _intent_request(prompt, payload)
    return request, True, cleaned_prompt


def _request_body(request: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    path = request.get("path")
    if not isinstance(path, str):
        path = "/v1/decide" if "question" in request else "/v1/screen"
    if path not in {"/v1/decide", "/v1/screen"}:
        raise ValueError("hook path must be /v1/decide or /v1/screen")
    if path == "/v1/decide":
        body = {"state": request.get("state"), "question": request.get("question")}
    else:
        body = {
            "criterion": request.get("criterion"),
            "candidates": request.get("candidates"),
            "top_n": request.get("top_n", 10),
            "tier": request.get("tier", "auto"),
        }
    task_id = str(request.get("task_id") or request.get("session_id") or "")
    return path, task_id, body


def _daemon_target() -> tuple[str, int]:
    host = os.environ.get("ELJEV_HOST", DEFAULT_HOST)
    raw_port = os.environ.get("ELJEV_PORT", str(DEFAULT_PORT))
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("ELJEV_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise ValueError("ELJEV_PORT must be in range 1..65535")
    return host, port


def _spawn_stamp_path() -> Path:
    if eljev_config is None:
        return REPO_ROOT / ".eljev" / SPAWN_STAMP_NAME
    return eljev_config.eljev_dir() / SPAWN_STAMP_NAME


def _spawn_daemon_once() -> None:
    stamp = _spawn_stamp_path()
    now = time.time()
    try:
        if stamp.exists() and now - stamp.stat().st_mtime < SPAWN_DEBOUNCE_SECONDS:
            return
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(f"{int(now)}\n", encoding="ascii")
    except OSError:
        return

    environment = dict(os.environ)
    root = str(REPO_ROOT)
    existing = environment.get("PYTHONPATH", "")
    entries = [entry for entry in existing.split(os.pathsep) if entry]
    if root not in entries:
        entries.insert(0, root)
    environment["PYTHONPATH"] = os.pathsep.join(entries)

    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "cwd": str(REPO_ROOT),
        "env": environment,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        kwargs["start_new_session"] = True

    try:
        process = subprocess.Popen([sys.executable, "-m", "eljev", "_daemon-run"], **kwargs)
    except OSError:
        return
    _log("daemon_spawned", pid=process.pid)


def _http_json(path: str, payload: dict[str, Any], deadline: float) -> dict[str, Any]:
    host, port = _daemon_target()
    body = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    request = (
        f"POST {path} HTTP/1.1\r\n"
        f"Host: {DEFAULT_HOST}:{port}\r\n"
        "Accept: application/json\r\n"
        "Content-Type: application/json\r\n"
        "Connection: close\r\n"
        f"Content-Length: {len(body)}\r\n"
        "\r\n"
    ).encode("ascii") + body

    connect_timeout = min(0.1, _remaining_seconds(deadline))
    try:
        connection = socket.create_connection((host, port), timeout=connect_timeout)
    except (ConnectionRefusedError, TimeoutError, OSError) as exc:
        raise _DaemonUnavailable() from exc

    with connection:
        connection.settimeout(_remaining_seconds(deadline))
        connection.sendall(request)
        response = bytearray()
        while True:
            connection.settimeout(_remaining_seconds(deadline))
            chunk = connection.recv(65_536)
            if not chunk:
                break
            response.extend(chunk)
            if len(response) > MAX_RESPONSE_BYTES:
                raise ValueError("daemon response exceeded max size")
            if b"\r\n\r\n" in response:
                headers, _, body_bytes = response.partition(b"\r\n\r\n")
                content_length = None
                for line in headers.split(b"\r\n")[1:]:
                    if line.lower().startswith(b"content-length:"):
                        content_length = int(line.split(b":", 1)[1].strip())
                        break
                if content_length is not None and len(body_bytes) >= content_length:
                    break

    header_bytes, separator, body_bytes = bytes(response).partition(b"\r\n\r\n")
    if not separator:
        raise ValueError("daemon returned an incomplete HTTP response")
    status_line = header_bytes.split(b"\r\n", 1)[0].decode("ascii", "replace")
    try:
        status_code = int(status_line.split(" ", 2)[1])
    except (IndexError, ValueError) as exc:
        raise ValueError("daemon returned an invalid HTTP status") from exc
    if status_code < 200 or status_code >= 300:
        raise ValueError(f"daemon returned HTTP {status_code}")
    try:
        decoded = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("daemon returned malformed JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("daemon returned a non-object JSON body")
    return decoded


def _oracle_override() -> dict[str, Any] | None:
    if os.environ.get("ELJEV_ORACLE_MODE") != "1":
        return None
    raw = os.environ.get("ELJEV_ORACLE_VERDICT_JSON", "")
    if not raw:
        return None
    value = json.loads(raw)
    return value if isinstance(value, dict) else None


def _format_probability_items(probabilities: dict[str, Any]) -> str | None:
    scored: list[tuple[str, float]] = []
    for key, value in probabilities.items():
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            scored.append((str(key), float(value)))
    if not scored:
        return None
    scored.sort(key=lambda item: item[1], reverse=True)
    top = scored[:3]
    return ", ".join(f"{name}={score:.3f}" for name, score in top)


def _decision_block(decision: dict[str, Any]) -> str:
    kind = decision.get("kind")
    if not isinstance(kind, str):
        kind = str(decision.get("shape", "decision"))
    selected = decision.get("choice")
    if selected is None and decision.get("score") is not None:
        selected = decision.get("score")
    confidence = decision.get("confidence")
    margin = decision.get("margin")
    if margin is None:
        margin = decision.get("margin_calibrated")
    if margin is None:
        margin = decision.get("margin_raw")
    status = decision.get("status")
    if not isinstance(status, str):
        status = "needs_review"
    exit_code = decision.get("exit_code")
    if not isinstance(exit_code, int):
        exit_code = 2

    summary_parts = [f"kind: {kind}"]
    if selected is not None:
        summary_parts.append(f"decision: {selected}")
    if isinstance(confidence, (int, float)):
        summary_parts.append(f"confidence: {float(confidence):.2f}")
    if isinstance(margin, (int, float)):
        summary_parts.append(f"margin: {float(margin):.2f}")

    if status == "selected" and exit_code == 0:
        gate_line = "gate: selected (exit 0) -> high-confidence calibrated decision"
    else:
        gate_line = f"gate: {status} (exit {exit_code}) -> treat as a hint; reason normally"

    lines = [
        "[el-jev advisory]",
        " | ".join(summary_parts),
        gate_line,
    ]
    probabilities = decision.get("probabilities")
    if isinstance(probabilities, dict):
        top = _format_probability_items(probabilities)
        if top is not None:
            lines.append(f"probabilities: {top}")
    lines.append("[/el-jev advisory]")
    return "\n".join(lines)


def _decision_allows_injection(decision: dict[str, Any]) -> bool:
    status = decision.get("status")
    return isinstance(status, str) and status in {"selected", "needs_review", "abstain_tie"}


def main() -> int:
    payload = _read_payload()
    if payload is None:
        _emit({})
        return 0
    if eljev_config is None:
        _emit({})
        return 0

    enabled, _ = eljev_config.enabled_state()
    if not enabled:
        _emit({})
        return 0

    started = time.perf_counter()
    try:
        mode = eljev_config.hook_mode()
        request, is_native, cleaned_prompt = _request_from_payload(payload, mode)
        path, task_id, body = _request_body(request)
        decision = _oracle_override()
        from_oracle = decision is not None
        if decision is None:
            deadline = time.monotonic() + (_timeout_ms() / 1000.0)
            try:
                decision = _http_json(path, body, deadline)
            except _DaemonUnavailable:
                _spawn_daemon_once()
                _emit({})
                return 0
        if not isinstance(decision, dict) or decision.get("schema") != DECISION_SCHEMA:
            raise ValueError("daemon returned an invalid decision schema")
        if not _decision_allows_injection(decision):
            _emit({})
            return 0

        _log(
            "decision",
            task_id=task_id or None,
            kind=decision.get("kind"),
            status=decision.get("status"),
            exit_code=decision.get("exit_code"),
            confidence=decision.get("confidence"),
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
            oracle=from_oracle,
        )

        if is_native and isinstance(cleaned_prompt, str):
            block = _decision_block(decision)
            _emit({"modifiedTransformedPrompt": (cleaned_prompt + "\n\n" + block).strip()})
            return 0
        _emit({"schema": "eljev.hook_result/1", "task_id": task_id or None, "decision": decision})
        return 0
    except _NoRequest:
        _emit({})
        return 0
    except Exception as error:
        _log(
            "fail_open",
            error_kind=type(error).__name__,
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 3),
        )
        _emit({})
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
