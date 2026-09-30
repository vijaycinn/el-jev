"""Command-line interface for el-jev.

Argument parsing is deliberately hand-written so ordinary screen calls do not
import ``argparse``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit

from . import config
from .client import EljevClient


VERSION = "0.2.0"
START_TIMEOUT_S = 8.0
STOP_TIMEOUT_S = 3.0


class CLIInputError(ValueError):
    """The command line or an input file violates the public contract."""


def _emit(value: object) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")


def _invalid(message: str) -> int:
    _emit({"error_kind": "invalid_input", "message": message, "exit_code": 1})
    return 1


def _exit_code(value: object, default: int = 2) -> int:
    if isinstance(value, dict):
        code = value.get("exit_code")
        if isinstance(code, int) and code in {0, 1, 2}:
            return code
        if value.get("status") == "ok":
            return 0
    return default


def _parse_options(args: list[str], value_options: set[str], flag_options: set[str]) -> dict[str, object]:
    values: dict[str, object] = {}
    index = 0
    while index < len(args):
        token = args[index]
        if not token.startswith("--"):
            raise CLIInputError(f"unexpected argument: {token}")
        option = token[2:]
        if "=" in option:
            name, value = option.split("=", 1)
            if name not in value_options:
                raise CLIInputError(f"unknown option: --{name}")
            if name in values:
                raise CLIInputError(f"option repeated: --{name}")
            values[name] = value
            index += 1
            continue
        if option in flag_options:
            if option in values:
                raise CLIInputError(f"option repeated: --{option}")
            values[option] = True
            index += 1
            continue
        if option not in value_options:
            raise CLIInputError(f"unknown option: --{option}")
        if option in values:
            raise CLIInputError(f"option repeated: --{option}")
        if index + 1 >= len(args):
            raise CLIInputError(f"option requires a value: --{option}")
        values[option] = args[index + 1]
        index += 2
    return values


def _load_json_file(path: str) -> object:
    try:
        if path == "-":
            return json.load(sys.stdin)
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream)
    except OSError as exc:
        raise CLIInputError(f"cannot read {path}: {exc}") from exc
    except ValueError as exc:
        raise CLIInputError(f"{path} must contain valid JSON") from exc


def _read_pidfile_record(path: Path | None = None) -> dict[str, object] | None:
    """Return ``{"pid": int, "started_at": float|None}`` from a JSON or legacy pidfile."""

    target = path or config.pidfile_path()
    try:
        content = target.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        value = json.loads(content)
    except ValueError:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return {"pid": value, "started_at": None} if value > 0 else None
    if not isinstance(value, dict):
        return None
    pid = value.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return None
    started_at = value.get("started_at")
    if isinstance(started_at, bool) or not isinstance(started_at, (int, float)):
        started_at = None
    return {"pid": pid, "started_at": float(started_at) if started_at is not None else None}


def _read_pidfile(path: Path | None = None) -> int | None:
    record = _read_pidfile_record(path)
    return int(record["pid"]) if record is not None else None


def _process_start_epoch(pid: int) -> float | None:
    """Best-effort process creation time (epoch seconds); None when unknown."""

    if pid <= 0:
        return None
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            kernel32.GetProcessTimes.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            try:
                created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
                if not kernel32.GetProcessTimes(
                    handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
                ):
                    return None
            finally:
                kernel32.CloseHandle(handle)
            ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
            return ticks / 10_000_000 - 11_644_473_600
        except (OSError, AttributeError, ValueError):
            return None
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            start_ticks = int(stat.rsplit(")", 1)[1].split()[19])
            boot_time = next(
                float(line.split()[1])
                for line in Path("/proc/stat").read_text(encoding="ascii").splitlines()
                if line.startswith("btime ")
            )
            return boot_time + start_ticks / os.sysconf("SC_CLK_TCK")
        except (OSError, ValueError, IndexError, StopIteration):
            return None
    return None


def _pidfile_owns_process(record: dict[str, object]) -> bool:
    """True only when the live process is provably the daemon that wrote the pidfile."""

    pid = int(record["pid"])
    started_at = record.get("started_at")
    if not isinstance(started_at, float):
        return False
    created = _process_start_epoch(pid)
    if created is None:
        return False
    # The daemon writes the pidfile after it starts; a recycled pid starts later.
    return created <= started_at + 2.0


def _remove_pidfile(path: Path | None = None) -> None:
    try:
        (path or config.pidfile_path()).unlink()
    except OSError:
        return


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_ACCESS_DENIED = 5
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        open_process.restype = ctypes.c_void_p
        get_exit_code_process = kernel32.GetExitCodeProcess
        get_exit_code_process.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        get_exit_code_process.restype = ctypes.c_int
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int

        handle = open_process(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
        if not handle:
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        try:
            exit_code = ctypes.c_uint32(0)
            if not get_exit_code_process(handle, ctypes.byref(exit_code)):
                return ctypes.get_last_error() == ERROR_ACCESS_DENIED
            return exit_code.value == STILL_ACTIVE
        finally:
            close_handle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _build_daemon_env() -> dict[str, str]:
    environment = dict(os.environ)
    root = str(config.repo_root())
    existing = environment.get("PYTHONPATH", "")
    entries = [entry for entry in existing.split(os.pathsep) if entry]
    if root not in entries:
        entries.insert(0, root)
    environment["PYTHONPATH"] = os.pathsep.join(entries)
    return environment


def _spawn_daemon() -> int:
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "cwd": str(config.repo_root()),
        "env": _build_daemon_env(),
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen([sys.executable, "-m", "eljev", "_daemon-run"], **kwargs)
    return process.pid


def _health_ready(client: EljevClient) -> dict[str, object] | None:
    health = client.health()
    if health.get("status") == "ok":
        return health
    return None


def _extract_pid(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    if isinstance(value, str):
        try:
            parsed = int(value)
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def _wait_for_pid_exit(pid: int, timeout_s: float) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return True
        time.sleep(0.05)
    return not _pid_exists(pid)


def _terminate_pid(pid: int) -> bool:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return _wait_for_pid_exit(pid, 1.0)


def _daemon_start_result() -> tuple[int, dict[str, object]]:
    client = EljevClient()
    ready = _health_ready(client)
    if ready is not None:
        return 0, ready

    spawned_pid = _spawn_daemon()
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        ready = _health_ready(client)
        if ready is not None:
            return 0, ready
        if not _pid_exists(spawned_pid):
            break
        time.sleep(0.05)

    if _pid_exists(spawned_pid):
        notes = [
            "daemon did not become healthy before the startup timeout",
            "check ELJEV_COHERE_ENDPOINT/ELJEV_COHERE_DEPLOYMENT and daemon logs",
        ]
    else:
        notes = [
            "daemon process exited before becoming healthy",
            "check ELJEV_COHERE_ENDPOINT/ELJEV_COHERE_DEPLOYMENT and daemon logs",
        ]
    return (
        2,
        {
            "status": "starting",
            "pid": spawned_pid,
            "error_kind": "daemon_starting",
            "notes": notes,
            "exit_code": 2,
        },
    )


def _daemon_start() -> int:
    code, payload = _daemon_start_result()
    _emit(payload)
    return code


def _daemon_stop_result() -> tuple[int, dict[str, object]]:
    client = EljevClient()
    health = client.health()
    pidfile_path = config.pidfile_path()
    pidfile_pid = _read_pidfile(pidfile_path)
    health_pid = _extract_pid(health.get("pid")) if isinstance(health, dict) else None

    stopped_pid = health_pid or pidfile_pid

    if isinstance(health, dict) and health.get("status") == "ok":
        shutdown_result = client.shutdown()
        if shutdown_result.get("status") == "down":
            return (
                2,
                {
                    "status": "error",
                    "pid": stopped_pid,
                    "error_kind": shutdown_result.get("error_kind", "connection"),
                    "notes": list(shutdown_result.get("notes", [])),
                    "exit_code": 2,
                },
            )
        if health_pid is not None and not _wait_for_pid_exit(health_pid, STOP_TIMEOUT_S):
            if not _terminate_pid(health_pid):
                return (
                    2,
                    {
                        "status": "stopping",
                        "pid": health_pid,
                        "error_kind": "timeout",
                        "notes": ["daemon did not exit after shutdown request"],
                        "exit_code": 2,
                    },
                )
        if pidfile_pid is not None and not _pid_exists(pidfile_pid):
            _remove_pidfile(pidfile_path)
        elif pidfile_pid == health_pid:
            _remove_pidfile(pidfile_path)
        return 0, {"status": "stopped", "pid": health_pid}

    if pidfile_pid is not None:
        record = _read_pidfile_record(pidfile_path)
        if _pid_exists(pidfile_pid) and record is not None and _pidfile_owns_process(record):
            if not _terminate_pid(pidfile_pid):
                return (
                    2,
                    {
                        "status": "error",
                        "pid": pidfile_pid,
                        "error_kind": "connection",
                        "notes": ["failed to terminate daemon process from pidfile"],
                        "exit_code": 2,
                    },
                )
            _remove_pidfile(pidfile_path)
            return 0, {"status": "stopped", "pid": pidfile_pid}
        # Dead pid, or a live process we cannot prove is our daemon (pid reuse): never kill it.
        _remove_pidfile(pidfile_path)
        payload: dict[str, object] = {"status": "stopped", "pid": pidfile_pid, "stale_pidfile": True}
        if _pid_exists(pidfile_pid):
            payload["notes"] = ["pidfile pid is alive but not provably el-jev; left untouched"]
        return 0, payload

    return 0, {"status": "stopped", "notes": ["daemon is not running"]}


def _daemon_stop() -> int:
    code, payload = _daemon_stop_result()
    _emit(payload)
    return code


def _daemon_status_result() -> tuple[int, dict[str, object]]:
    pidfile_pid = _read_pidfile()
    stale_pidfile = False
    if pidfile_pid is not None:
        record = _read_pidfile_record()
        # A live pid we cannot prove is el-jev is a recycled pid, i.e. a stale pidfile.
        if not _pid_exists(pidfile_pid) or record is None or (
            record.get("started_at") is not None and not _pidfile_owns_process(record)
        ):
            stale_pidfile = True
            _remove_pidfile()
            pidfile_pid = None

    health = EljevClient().health()
    health_pid = _extract_pid(health.get("pid")) if isinstance(health, dict) else None
    active_pid = health_pid or pidfile_pid

    if isinstance(health, dict) and health.get("status") == "ok":
        payload = dict(health)
        if active_pid is not None:
            payload["pid"] = active_pid
        if pidfile_pid is not None:
            payload["pidfile_pid"] = pidfile_pid
        if stale_pidfile:
            payload["stale_pidfile"] = True
        return 0, payload

    if pidfile_pid is not None:
        return (
            2,
            {
                "status": "starting",
                "pid": pidfile_pid,
                "pidfile_pid": pidfile_pid,
                "health": health,
                "stale_pidfile": stale_pidfile,
                "exit_code": 2,
            },
        )

    if stale_pidfile:
        return 0, {"status": "stopped", "stale_pidfile": True}
    return 0, {"status": "stopped", "health": health}


def _daemon_status() -> int:
    code, payload = _daemon_status_result()
    _emit(payload)
    return code


def _screen(args: list[str]) -> int:
    options = _parse_options(
        args,
        value_options={"criterion", "candidates", "text", "top-n"},
        flag_options={"json"},
    )
    criterion = options.get("criterion")
    if not isinstance(criterion, str) or not criterion.strip():
        raise CLIInputError("--criterion is required and must be non-empty")
    candidates_path = options.get("candidates")
    text = options.get("text")
    if (candidates_path is None) == (text is None):
        raise CLIInputError("provide exactly one of --candidates or --text")
    if text is not None:
        candidates = [{"id": "c0", "text": text}]
    else:
        candidates = _load_json_file(str(candidates_path))
    top_n: object | None = None
    if "top-n" in options:
        try:
            top_n = int(str(options["top-n"]))
        except ValueError as exc:
            raise CLIInputError("--top-n must be an integer") from exc

    result = EljevClient().screen(criterion, candidates, top_n=top_n)
    _emit(result)
    return _exit_code(result)


def _decide(args: list[str]) -> int:
    options = _parse_options(args, value_options={"state", "question", "state-text"}, flag_options=set())
    state_path = options.get("state")
    state_text = options.get("state-text")
    question_path = options.get("question")
    if (state_path is None) == (state_text is None):
        raise CLIInputError("provide exactly one of --state or --state-text")
    if not isinstance(question_path, str) or not question_path:
        raise CLIInputError("--question is required")
    state = state_text if state_text is not None else _load_json_file(str(state_path))
    question = _load_json_file(question_path)
    result = EljevClient().decide(state, question)
    _emit(result)
    return _exit_code(result)


def _coerce_positive_int(raw: object, label: str) -> int:
    try:
        value = int(str(raw))
    except ValueError as exc:
        raise CLIInputError(f"{label} must be an integer") from exc
    if value <= 0:
        raise CLIInputError(f"{label} must be greater than zero")
    return value


def _show_log(args: list[str]) -> int:
    limit = 10
    positionals: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--limit":
            if index + 1 >= len(args):
                raise CLIInputError("--limit requires a value")
            limit = _coerce_positive_int(args[index + 1], "--limit")
            index += 2
            continue
        if token.startswith("--limit="):
            limit = _coerce_positive_int(token.split("=", 1)[1], "--limit")
            index += 1
            continue
        if token.startswith("--"):
            raise CLIInputError(f"unknown option: {token}")
        positionals.append(token)
        index += 1
    if len(positionals) > 1:
        raise CLIInputError("log accepts at most one positional limit")
    if positionals:
        limit = _coerce_positive_int(positionals[0], "limit")
    result = EljevClient().log(limit)
    _emit(result)
    return 0


def _status() -> int:
    enabled, source = config.enabled_state()
    _, daemon = _daemon_status_result()
    payload = {
        "enabled": enabled,
        "source": source,
        "hook_mode": config.hook_mode(),
        "daemon_status": daemon.get("status"),
        "daemon_pid": daemon.get("pid"),
        "daemon": daemon,
        "cohere_endpoint_configured": bool(config.cohere_endpoint()),
        "cohere_deployment": config.cohere_deployment(),
        "auth_mode": config.auth_mode(),
        "azure_subscription": config.azure_subscription() or None,
        "coverage_policy": config.coverage_policy(),
        "logging_enabled": config.logging_enabled(),
        "config_path": str(config.config_path()),
        "log_dir": str(config.log_dir()),
    }
    _emit(payload)
    return 0


def _set_enabled(enabled: bool) -> int:
    config.save_config({"enabled": enabled})
    daemon_code, daemon_payload = (
        _daemon_start_result() if enabled else _daemon_stop_result()
    )
    effective, source = config.enabled_state()
    payload: dict[str, object] = {
        "status": "enabled" if enabled else "disabled",
        "effective": effective,
        "source": source,
        "daemon": daemon_payload,
    }
    if effective != enabled:
        payload["notes"] = [f"effective state is overridden by {source}"]
    _emit(payload)
    return daemon_code


def _validate_endpoint(raw: str) -> str:
    value = raw.strip().rstrip("/")
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.netloc:
        raise CLIInputError("--endpoint must be an https:// URL with a host")
    return value


def _configure(args: list[str]) -> int:
    options = _parse_options(
        args,
        value_options={"endpoint", "deployment", "policy", "auth", "hook-mode", "subscription", "hook-timeout-ms"},
        flag_options=set(),
    )
    if not options:
        raise CLIInputError("configure requires at least one option")

    updates: dict[str, object] = {}
    if "endpoint" in options:
        endpoint = options["endpoint"]
        if not isinstance(endpoint, str):
            raise CLIInputError("--endpoint must be a string")
        updates["cohere_endpoint"] = _validate_endpoint(endpoint)
    if "deployment" in options:
        deployment = str(options["deployment"]).strip()
        if not deployment:
            raise CLIInputError("--deployment must be non-empty")
        updates["cohere_deployment"] = deployment
    if "policy" in options:
        policy = str(options["policy"]).strip()
        if policy not in config.VALID_POLICIES:
            raise CLIInputError(f"--policy must be one of: {', '.join(config.VALID_POLICIES)}")
        updates["coverage_policy"] = policy
    if "auth" in options:
        auth = str(options["auth"]).strip().lower()
        if auth not in config.VALID_AUTH:
            raise CLIInputError(f"--auth must be one of: {', '.join(config.VALID_AUTH)}")
        updates["auth"] = auth
    if "subscription" in options:
        subscription = str(options["subscription"]).strip()
        if subscription.lower() in {"", "default", "none"}:
            subscription = ""
        elif not re.fullmatch(r"[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", subscription):
            raise CLIInputError("--subscription must be a subscription GUID or 'default'")
        updates["subscription"] = subscription
    if "hook-mode" in options:
        mode = str(options["hook-mode"]).strip().lower()
        if mode not in config.VALID_HOOK_MODES:
            raise CLIInputError(f"--hook-mode must be one of: {', '.join(config.VALID_HOOK_MODES)}")
        updates["hook_mode"] = mode
    if "hook-timeout-ms" in options:
        try:
            timeout_ms = int(str(options["hook-timeout-ms"]).strip())
            if timeout_ms <= 0:
                raise ValueError
        except ValueError:
            raise CLIInputError("--hook-timeout-ms must be a positive integer")
        updates["hook_timeout_ms"] = timeout_ms

    saved = config.save_config(updates)
    _emit(
        {
            "status": "configured",
            "config": {
                "enabled": bool(saved.get("enabled", True)),
                "cohere_endpoint": config.cohere_endpoint(),
                "cohere_endpoint_configured": bool(config.cohere_endpoint()),
                "cohere_deployment": config.cohere_deployment(),
                "coverage_policy": config.coverage_policy(),
                "auth": config.auth_mode(),
                "subscription": config.azure_subscription() or None,
                "hook_mode": config.hook_mode(),
            },
            "config_path": str(config.config_path()),
        }
    )
    return 0


def _parse_scope(options: dict[str, object]) -> str:
    scope = str(options.get("scope", "repo")).strip().lower()
    if scope not in {"repo", "user"}:
        raise CLIInputError("--scope must be repo or user")
    return scope


def _hook_config_path(scope: str, repo: str | None) -> Path:
    if scope == "repo":
        root = Path(repo).resolve() if repo else Path.cwd().resolve()
        return root / ".github" / "hooks" / "eljev.json"
    copilot_home = os.environ.get("COPILOT_HOME")
    if copilot_home and copilot_home.strip():
        base = Path(copilot_home).expanduser().resolve()
    else:
        base = Path.home() / ".copilot"
    return base / "hooks" / "eljev.json"


def _hook_command_block(timeout_s: float) -> dict[str, object]:
    python_path = Path(sys.executable).resolve().as_posix()
    hook_path = (config.repo_root() / "hooks" / "eljev_pre_turn.py").resolve().as_posix()
    quoted_python = f"\"{python_path}\""
    quoted_hook = f"\"{hook_path}\""
    return {
        "version": 1,
        "hooks": {
            "userPromptTransformed": [
                {
                    "type": "command",
                    "bash": f"{quoted_python} {quoted_hook}",
                    "powershell": f"& {quoted_python} {quoted_hook}",
                    "timeoutSec": int(timeout_s) if float(timeout_s).is_integer() else timeout_s,
                    "comment": "el-jev pre-turn decision hook (installed by eljev install-hook)",
                }
            ]
        },
    }


def _parse_timeout_seconds(raw: object) -> float:
    try:
        value = float(str(raw))
    except ValueError as exc:
        raise CLIInputError("--timeout-sec must be a number") from exc
    if value <= 0:
        raise CLIInputError("--timeout-sec must be greater than zero")
    return value


def _install_hook(args: list[str]) -> int:
    options = _parse_options(
        args,
        value_options={"scope", "repo", "timeout-sec"},
        flag_options={"force"},
    )
    scope = _parse_scope(options)
    repo = str(options["repo"]) if "repo" in options else None
    timeout_s = _parse_timeout_seconds(options.get("timeout-sec", 2))
    force = bool(options.get("force", False))
    target = _hook_config_path(scope, repo)
    if target.exists() and not force:
        raise CLIInputError(f"hook file exists: {target} (use --force to overwrite)")

    payload = _hook_command_block(timeout_s)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n",
        encoding="utf-8",
    )
    result: dict[str, object] = {"status": "installed", "path": str(target)}
    if scope == "repo":
        result["notes"] = [
            "this file contains absolute local paths; add .github/hooks/eljev.json to .gitignore before committing"
        ]
    _emit(result)
    return 0


def _is_eljev_hook_file(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    hooks = value.get("hooks")
    if not isinstance(hooks, dict):
        return False
    entries = hooks.get("userPromptTransformed")
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        for key in ("bash", "powershell"):
            command = entry.get(key)
            if isinstance(command, str) and "eljev_pre_turn.py" in command.replace("\\", "/"):
                return True
    return False


def _uninstall_hook(args: list[str]) -> int:
    options = _parse_options(
        args,
        value_options={"scope", "repo"},
        flag_options=set(),
    )
    scope = _parse_scope(options)
    repo = str(options["repo"]) if "repo" in options else None
    target = _hook_config_path(scope, repo)
    if not target.exists():
        _emit({"status": "absent", "path": str(target)})
        return 0
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise CLIInputError(f"refusing to remove non-JSON hook file: {target}")
    if not _is_eljev_hook_file(loaded):
        raise CLIInputError(f"refusing to remove unrelated hook file: {target}")
    target.unlink()
    _emit({"status": "uninstalled", "path": str(target)})
    return 0


def _help() -> int:
    sys.stdout.write(
        "eljev on|enable                          (enable automatic routing and start daemon)\n"
        "eljev off|disable                        (disable automatic routing and stop daemon)\n"
        "eljev status                             (show effective config and daemon status)\n"
        "eljev configure [--endpoint URL] [--deployment NAME] [--policy P] [--auth M] [--subscription ID] [--hook-mode H]\n"
        "eljev install-hook [--scope repo|user] [--repo DIR] [--timeout-sec N] [--force]\n"
        "eljev uninstall-hook [--scope repo|user] [--repo DIR]\n"
        "eljev log [N] [--limit N]               (show last N decisions)\n"
        "eljev screen --criterion \"...\" --candidates candidates.json [--top-n K] [--json]\n"
        "eljev screen --criterion \"...\" --text \"one candidate\"\n"
        "eljev decide --state state.json --question question.json\n"
        "eljev decide --state-text \"...\" --question question.json\n"
        "eljev health\n"
        "eljev daemon start|stop|status\n"
        "eljev version\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] in {"-h", "--help"}:
        return _help()
    if arguments[0] == "_daemon-run":
        from .daemon import run

        return int(run() or 0)
    command = arguments[0]
    try:
        if command in {"on", "enable"}:
            return _set_enabled(True)
        if command in {"off", "disable"}:
            return _set_enabled(False)
        if command == "status":
            return _status()
        if command == "configure":
            return _configure(arguments[1:])
        if command == "install-hook":
            return _install_hook(arguments[1:])
        if command == "uninstall-hook":
            return _uninstall_hook(arguments[1:])
        if command == "log":
            return _show_log(arguments[1:])
        if command == "screen":
            return _screen(arguments[1:])
        if command == "decide":
            return _decide(arguments[1:])
        if command == "health":
            if len(arguments) != 1:
                raise CLIInputError("health does not accept options")
            result = EljevClient().health()
            _emit(result)
            return 0 if result.get("status") == "ok" else 2
        if command == "daemon":
            if len(arguments) != 2 or arguments[1] not in {"start", "stop", "status"}:
                raise CLIInputError("daemon requires start, stop, or status")
            return {
                "start": _daemon_start,
                "stop": _daemon_stop,
                "status": _daemon_status,
            }[arguments[1]]()
        if command == "version":
            if len(arguments) != 1:
                raise CLIInputError("version does not accept options")
            _emit({"version": VERSION})
            return 0
        raise CLIInputError(f"unknown command: {command}")
    except CLIInputError as exc:
        return _invalid(str(exc))
    except ValueError as exc:
        return _invalid(str(exc))


__all__ = ["main"]
