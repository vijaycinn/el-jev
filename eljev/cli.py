"""Command-line interface for el-jev.

Argument parsing is deliberately hand-written so ordinary screen calls do not
import ``argparse``.
"""

from __future__ import annotations

import json
import os
import sys
import time

from .client import EljevClient


VERSION = "0.1.0"
PIDFILE_NAME = "daemon.pid"
START_TIMEOUT_S = 8.0


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


def _eljev_dir() -> str:
    env_dir = os.environ.get("ELJEV_DIR")
    if env_dir:
        return env_dir
    custom_home = os.environ.get("USERPROFILE") or os.environ.get("HOME")
    if custom_home and any(k in custom_home.lower() for k in ("temp", "tmp")):
        target = os.path.join(custom_home, ".eljev")
        os.makedirs(target, exist_ok=True)
        return target
    local_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".eljev")
    os.makedirs(local_dir, exist_ok=True)
    return local_dir


def _pidfile_path() -> str:
    return os.path.join(_eljev_dir(), PIDFILE_NAME)


def _read_pidfile(path: str | None = None) -> int | None:
    target = path or _pidfile_path()
    try:
        with open(target, "r", encoding="ascii") as stream:
            value = int(stream.read().strip())
    except (OSError, TypeError, ValueError):
        return None
    return value if value > 0 else None


def _write_pidfile(pid: int, path: str | None = None) -> None:
    target = path or _pidfile_path()
    directory = os.path.dirname(target)
    os.makedirs(directory, exist_ok=True)
    temporary = target + ".tmp"
    with open(temporary, "w", encoding="ascii", newline="\n") as stream:
        stream.write(str(pid))
        stream.write("\n")
    os.replace(temporary, target)


def _remove_pidfile(path: str | None = None) -> None:
    try:
        os.remove(path or _pidfile_path())
    except FileNotFoundError:
        return
    except OSError:
        return


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _spawn_daemon() -> int:
    import subprocess

    flags = (
        getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "eljev", "_daemon-run"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )
    _write_pidfile(process.pid)
    return process.pid


def _health_ready(client: EljevClient) -> dict[str, object] | None:
    health = client.health()
    if health.get("status") == "ok":
        return health
    return None


def _daemon_start() -> int:
    pid = _read_pidfile()
    client = EljevClient()
    if pid is not None:
        if not _pid_exists(pid):
            _remove_pidfile()
        else:
            ready = _health_ready(client)
            if ready is not None:
                _emit(ready)
                return 0
            _emit(
                {
                    "status": "starting",
                    "pid": pid,
                    "error_kind": "daemon_starting",
                    "notes": ["daemon process exists but health is not ready"],
                }
            )
            return 2

    pid = _spawn_daemon()
    deadline = time.monotonic() + START_TIMEOUT_S
    while time.monotonic() < deadline:
        ready = _health_ready(client)
        if ready is not None:
            _emit(ready)
            return 0
        if not _pid_exists(pid):
            break
        time.sleep(0.05)
    if not _pid_exists(pid):
        _remove_pidfile()
    _emit(
        {
            "status": "starting",
            "pid": pid,
            "error_kind": "daemon_starting",
            "notes": ["daemon did not become healthy before the startup timeout"],
        }
    )
    return 2


def _daemon_stop() -> int:
    pid = _read_pidfile()
    if pid is None:
        _remove_pidfile()
        _emit({"status": "stopped", "notes": ["daemon is not running"]})
        return 0
    if not _pid_exists(pid):
        _remove_pidfile()
        _emit({"status": "stopped", "pid": pid, "stale_pidfile": True})
        return 0

    import signal

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _remove_pidfile()
        _emit({"status": "stopped", "pid": pid, "stale_pidfile": True})
        return 0
    except OSError as exc:
        _emit({"status": "error", "pid": pid, "error_kind": "connection", "notes": [str(exc)]})
        return 2

    deadline = time.monotonic() + 2.0
    while _pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    if _pid_exists(pid):
        _emit(
            {
                "status": "stopping",
                "pid": pid,
                "error_kind": "timeout",
                "notes": ["daemon did not exit after termination was requested"],
            }
        )
        return 2
    _remove_pidfile()
    _emit({"status": "stopped", "pid": pid})
    return 0


def _daemon_status() -> int:
    pid = _read_pidfile()
    if pid is not None and not _pid_exists(pid):
        _remove_pidfile()
        _emit({"status": "stopped", "pid": pid, "stale_pidfile": True})
        return 0

    health = EljevClient().health()
    if health.get("status") == "ok":
        if pid is not None:
            health = dict(health)
            health["pid"] = pid
        _emit(health)
        return 0
    if pid is not None:
        health = dict(health)
        health["status"] = "starting"
        health["pid"] = pid
        _emit(health)
        return 2
    _emit({"status": "stopped", "notes": ["daemon is not running"]})
    return 0


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
        raw_top_n = options["top-n"]
        try:
            top_n = int(str(raw_top_n))
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


def _config_path() -> str:
    directory = _eljev_dir()
    os.makedirs(directory, exist_ok=True)
    return os.path.join(directory, "config.json")


def _set_enabled(enabled: bool) -> int:
    path = _config_path()
    config: dict[str, object] = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                if isinstance(loaded, dict):
                    config = loaded
        except Exception:
            config = {}
    config["enabled"] = enabled
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)

    if enabled:
        daemon_res = _daemon_start()
        _emit({"status": "enabled", "hook_active": True, "daemon_exit": daemon_res})
        return daemon_res
    else:
        daemon_res = _daemon_stop()
        _emit({"status": "disabled", "hook_active": False, "daemon_exit": daemon_res})
        return 0


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


def _toggle_status() -> int:
    path = _config_path()
    enabled = True
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                if isinstance(cfg, dict):
                    enabled = bool(cfg.get("enabled", True))
        except Exception:
            pass
    el_jev_env = _get_el_jev_env()
    if el_jev_env is not None:
        enabled = el_jev_env.lower() in {"on", "1", "true", "yes", "enable", "enabled"}

    health = EljevClient().health()
    pid = _read_pidfile()
    _emit({
        "enabled": enabled,
        "EL_JEV": el_jev_env,
        "daemon_pid": pid,
        "daemon_status": health.get("status", "stopped"),
    })
    return 0


def _help() -> int:
    sys.stdout.write(
        "eljev on|enable                 (enable automatic routing and start daemon)\n"
        "eljev off|disable               (disable automatic routing and stop daemon)\n"
        "eljev status                    (show hook and daemon status)\n"
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

        run()
        return 0
    command = arguments[0]
    try:
        if command in {"on", "enable"}:
            return _set_enabled(True)
        if command in {"off", "disable"}:
            return _set_enabled(False)
        if command == "status":
            return _toggle_status()
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
