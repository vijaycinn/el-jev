"""Fast, fail-open el-jev pre-turn hook.

The hook accepts either the native Copilot ``userPromptTransformed`` payload
with an embedded request marker, or the generic ``eljev.hook/1`` payload
documented in hooks/README.md.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any


HOOK_SCHEMA = "eljev.hook/1"
DECISION_SCHEMA = "eljev.decision/1"
DEFAULT_TIMEOUT_MS = 1500
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
MARKER_START = "<!--eljev.request:"
MARKER_END = "-->"
MAX_RESPONSE_BYTES = 1_048_576


def _eljev_dir() -> Path:
    env_dir = os.environ.get("ELJEV_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    local_dir = Path(__file__).resolve().parent.parent / ".eljev"
    local_dir.mkdir(parents=True, exist_ok=True)
    return local_dir


CONFIG_PATH = _eljev_dir() / "config.json"


def _get_el_jev_env() -> str | None:
    for var in ("EL_JEV", "ELJEV_ENABLED", "ELJEV_HOOK_ENABLED"):
        val = os.environ.get(var)
        if val is not None and val.strip():
            return val.strip()

    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
                val, _ = winreg.QueryValueEx(key, "EL_JEV")
                if val is not None and str(val).strip():
                    return str(val).strip()
        except Exception:
            pass

    return None


def _enabled() -> bool:
    env_val = _get_el_jev_env()
    if env_val is not None:
        s = env_val.lower()
        if s in {"off", "0", "false", "no", "disable", "disabled"}:
            return False
        if s in {"on", "1", "true", "yes", "enable", "enabled"}:
            return True

    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                return bool(cfg.get("enabled", True))
        except Exception:
            pass

    return True


def _timeout_ms() -> int:
    raw = os.environ.get("ELJEV_HOOK_TIMEOUT_MS", str(DEFAULT_TIMEOUT_MS))
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_MS
    return max(value, 1)


def _now_ms() -> float:
    return time.perf_counter() * 1000.0


def _log(event: str, **fields: Any) -> None:
    """Append diagnostics without ever writing secrets or candidate text."""
    try:
        log_dir = Path(
            os.environ.get("ELJEV_LOG_DIR", str(_eljev_dir() / "logs"))
        ).expanduser()
        log_dir.mkdir(parents=True, exist_ok=True)
        record = {
            "ts_ms": int(time.time() * 1000),
            "event": event,
            **fields,
        }
        with (log_dir / "hook.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(
                json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
            )
    except Exception:
        # Logging must never turn a fail-open hook into a failed turn.
        return


def _emit(value: dict[str, Any]) -> None:
    """Emit exactly one compact JSON object for Copilot's hook parser."""
    try:
        sys.stdout.write(
            json.dumps(value, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
            + "\n"
        )
        sys.stdout.flush()
    except Exception:
        return


def _read_payload() -> dict[str, Any] | None:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        value = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _native_prompt(payload: dict[str, Any]) -> tuple[str, str] | None:
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
    marker_json = prompt[body_start:end].strip()
    try:
        request = json.loads(marker_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(request, dict):
        return None
    cleaned = (prompt[:start] + prompt[end + len(MARKER_END) :]).strip()
    return json.dumps(request, ensure_ascii=True), cleaned


def _is_binary_question(text: str) -> bool:
    t = text.strip().lower()
    prefixes = (
        "is ",
        "is it",
        "should ",
        "can ",
        "can we",
        "could ",
        "does ",
        "does it",
        "will ",
        "would ",
        "are ",
        "was ",
    )
    if any(t.startswith(p) for p in prefixes):
        return True
    if "?" in t and any(k in t for k in ("safe to", "should i", "should we", "is it safe")):
        return True
    return False


def _auto_question_from_prompt(prompt: str) -> dict[str, Any]:
    cleaned = prompt.strip()
    if _is_binary_question(cleaned):
        return {
            "path": "/v1/decide",
            "state": cleaned,
            "question": {
                "id": "auto_gate",
                "kind": "noul",
                "instructions": cleaned,
                "criteria": {
                    "true": "Affirmative, approved, safe, recommended, or correct",
                    "false": "Negative, rejected, risky, not recommended, or incorrect",
                },
            },
        }
    else:
        return {
            "path": "/v1/decide",
            "state": cleaned,
            "question": {
                "id": "intent_choice",
                "kind": "choice",
                "instructions": "Classify the primary action intent for this request",
                "criteria": {
                    "code_modification": "Implement, write, edit, generate, refactor, or fix code in files",
                    "review_audit": "Perform code review, security review, PR critique, or safety audit",
                    "investigation_search": "Search codebase, find symbols/files, explore architecture, check logs or documentation",
                    "execution_testing": "Run terminal commands, builds, test suites, or deployment tasks",
                    "advisory_explanation": "Provide high-level architecture advice, conceptual explanations, or answering technical questions",
                },
            },
        }


def _request_from_payload(
    payload: dict[str, Any],
) -> tuple[dict[str, Any], bool, str | None]:
    if payload.get("schema") == HOOK_SCHEMA:
        return payload, False, None

    if isinstance(payload.get("criterion"), str) and "candidates" in payload:
        return payload, False, None

    native = _native_prompt(payload)
    if native is not None:
        request_json, cleaned_prompt = native
        request = json.loads(request_json)
        request["prompt"] = cleaned_prompt
        request["transformedPrompt"] = cleaned_prompt
        request["session_id"] = payload.get("sessionId") or payload.get("session_id")
        return request, True, cleaned_prompt

    # Automatic routing for direct user interactions without embedded markers
    raw_prompt = payload.get("transformedPrompt") or payload.get("prompt")
    if not isinstance(raw_prompt, str) or not raw_prompt.strip():
        raise ValueError("no prompt in payload")

    cleaned_prompt = raw_prompt.strip()
    tokens = cleaned_prompt.lower().split()
    if len(tokens) <= 2 and tokens[0] in {"hi", "hello", "hey", "thanks", "ok", "yes", "no", "bye"}:
        raise ValueError("trivial interaction")

    auto_req = _auto_question_from_prompt(cleaned_prompt)
    auto_req["session_id"] = payload.get("sessionId") or payload.get("session_id")
    return auto_req, True, cleaned_prompt


def _daemon_target() -> tuple[str, int]:
    raw_url = os.environ.get("ELJEV_HOOK_DAEMON_URL", "").strip()
    if raw_url:
        if not raw_url.startswith("http://"):
            raise ValueError("ELJEV_HOOK_DAEMON_URL must use http://")
        authority = raw_url[7:].split("/", 1)[0]
        if ":" in authority:
            host, port_text = authority.rsplit(":", 1)
            return host, int(port_text)
        return authority, DEFAULT_PORT
    host = os.environ.get("ELJEV_HOST", DEFAULT_HOST)
    port = int(os.environ.get("ELJEV_PORT", str(DEFAULT_PORT)))
    return host, port


def _remaining(deadline: float) -> float:
    remaining = deadline - time.perf_counter()
    if remaining <= 0:
        raise TimeoutError("hook deadline exceeded")
    return remaining


def _spawn_daemon_if_needed(host: str, port: int) -> None:
    if host not in {"127.0.0.1", "localhost"}:
        return
    import subprocess

    flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )
    repo_root = Path(__file__).resolve().parent.parent
    env = dict(os.environ)
    if "PYTHONPATH" not in env or str(repo_root) not in env["PYTHONPATH"]:
        env["PYTHONPATH"] = f"{repo_root};" + env.get("PYTHONPATH", "")
    if "ELJEV_COHERE_ENDPOINT" not in env:
        env["ELJEV_COHERE_ENDPOINT"] = "https://<your-resource>.services.ai.azure.com"
    if "ELJEV_COHERE_DEPLOYMENT" not in env:
        env["ELJEV_COHERE_DEPLOYMENT"] = "Cohere-rerank-v4.0-pro"

    try:
        subprocess.Popen(
            [sys.executable, "-m", "eljev", "_daemon-run"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=flags,
            cwd=str(repo_root),
            env=env,
        )
    except Exception:
        pass


def _http_json(
    method: str,
    path: str,
    payload: dict[str, Any],
    deadline: float,
) -> dict[str, Any]:
    host, port = _daemon_target()
    body = json.dumps(
        payload, ensure_ascii=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    request = (
        f"{method} {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Accept: application/json\r\n"
        "Content-Type: application/json\r\n"
        "Connection: close\r\n"
        f"Content-Length: {len(body)}\r\n"
        "\r\n"
    ).encode("ascii") + body

    try:
        connection = socket.create_connection((host, port), timeout=min(0.2, _remaining(deadline)))
    except (ConnectionRefusedError, OSError):
        _spawn_daemon_if_needed(host, port)
        time.sleep(0.15)
        connection = socket.create_connection((host, port), timeout=_remaining(deadline))

    with connection:
        connection.settimeout(_remaining(deadline))
        connection.sendall(request)
        chunks: list[bytes] = []
        total = 0
        while total < MAX_RESPONSE_BYTES:
            chunk = connection.recv(min(65_536, MAX_RESPONSE_BYTES - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if b"\r\n\r\n" in b"".join(chunks):
                header_end = b"".join(chunks).find(b"\r\n\r\n")
                headers = b"".join(chunks)[:header_end].lower()
                marker = b"content-length:"
                if marker in headers:
                    line = next(
                        line for line in headers.split(b"\r\n") if line.startswith(marker)
                    )
                    expected = int(line.split(b":", 1)[1].strip())
                    if total - header_end - 4 >= expected:
                        break

    raw = b"".join(chunks)
    header_raw, separator, body_raw = raw.partition(b"\r\n\r\n")
    if not separator:
        raise ValueError("daemon returned an incomplete HTTP response")
    status_line = header_raw.split(b"\r\n", 1)[0].decode("ascii", "replace")
    try:
        status_code = int(status_line.split(" ", 2)[1])
    except (IndexError, ValueError) as error:
        raise ValueError("daemon returned an invalid HTTP status") from error
    if status_code < 200 or status_code >= 300:
        raise ConnectionError(f"daemon returned HTTP {status_code}")
    try:
        value = json.loads(body_raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("daemon returned malformed JSON") from error
    if not isinstance(value, dict):
        raise ValueError("daemon returned a non-object JSON body")
    return value


def _request_body(request: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    path = request.get("path")
    if not isinstance(path, str):
        path = "/v1/decide" if "question" in request else "/v1/screen"
    if path not in {"/v1/screen", "/v1/decide"}:
        raise ValueError("hook path must be /v1/screen or /v1/decide")
    if path == "/v1/decide":
        body = {
            "state": request.get("state", ""),
            "question": request.get("question"),
        }
    else:
        body = {
            "criterion": request.get("criterion"),
            "candidates": request.get("candidates"),
            "top_n": request.get("top_n", 10),
            "tier": request.get("tier", "auto"),
        }
    return path, str(request.get("task_id") or request.get("session_id") or ""), body


def _oracle_override() -> dict[str, Any] | None:
    if os.environ.get("ELJEV_ORACLE_MODE") != "1":
        return None
    raw = os.environ.get("ELJEV_ORACLE_VERDICT_JSON", "")
    if not raw:
        return None
    value = json.loads(raw)
    return value if isinstance(value, dict) else None


def _decision_context(decision: dict[str, Any]) -> str:
    kind = decision.get("kind") or decision.get("shape", "decision")
    choice = decision.get("choice")
    conf = decision.get("confidence")
    probs = decision.get("probabilities")
    noul = decision.get("noul")
    status = decision.get("status", "selected")

    lines = [
        "[el-jev decision]",
        f"- type: {kind}",
        f"- decision: {choice}",
    ]
    if conf is not None:
        lines.append(f"- confidence: {conf}")
    if noul is not None:
        lines.append(f"- p(true): {noul}")
    if probs and isinstance(probs, dict):
        prob_str = ", ".join(f"{k}: {v}" for k, v in probs.items())
        lines.append(f"- probabilities: {{{prob_str}}}")
    lines.append(f"- status: {status}")
    lines.append("[/el-jev decision]")
    return "\n".join(lines)


def main() -> int:
    payload = _read_payload()
    if payload is None:
        _log("invalid_input")
        _emit({})
        return 0
    if not _enabled():
        _emit({})
        return 0

    started = _now_ms()
    try:
        request, native, cleaned_prompt = _request_from_payload(payload)
        path, task_id, body = _request_body(request)
        deadline = time.perf_counter() + (_timeout_ms() / 1000.0)
        decision = _oracle_override()
        if decision is None:
            decision = _http_json("POST", path, body, deadline)
        if decision.get("schema") != DECISION_SCHEMA:
            raise ValueError("daemon returned the wrong decision schema")

        _log(
            "decision",
            task_id=task_id or None,
            status=decision.get("status"),
            error_kind=decision.get("error_kind"),
            elapsed_ms=round(_now_ms() - started, 3),
            oracle=bool(_oracle_override()),
        )
        if native:
            assert cleaned_prompt is not None
            _emit(
                {
                    "modifiedTransformedPrompt": (
                        cleaned_prompt + "\n\n" + _decision_context(decision)
                    ).strip()
                }
            )
        else:
            _emit(
                {
                    "schema": "eljev.hook_result/1",
                    "task_id": task_id or None,
                    "decision": decision,
                }
            )
        return 0
    except Exception as error:
        _log(
            "fail_open",
            error_kind=type(error).__name__,
            elapsed_ms=round(_now_ms() - started, 3),
        )
        _emit({})
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
