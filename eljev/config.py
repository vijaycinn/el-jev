"""Shared configuration helpers for el-jev."""

from __future__ import annotations

from collections.abc import Mapping
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
from urllib.parse import urlsplit


DEFAULT_DEPLOYMENT = "Cohere-rerank-v4.0-pro"
DEFAULT_MD1_DEPLOYMENT = "Decision-1"
DEFAULT_ENGINE = "cohere"
DEFAULT_EXTENSION_MODE = "advisory"
DEFAULT_POLICY = "always_abstain_v0"
DEFAULT_HOOK_TIMEOUT_MS = 750
MD1_PATH = "/providers/microsoft/v1/systemone"
VALID_POLICIES = ("always_abstain_v0", "calibrated")
VALID_AUTH = ("azcli", "managed_identity")
VALID_HOOK_MODES = ("intent", "marker")
VALID_ENGINES = ("md1", "cohere")
VALID_EXTENSION_MODES = ("advisory", "enforce")
ALLOWED_MD1_HOST_SUFFIXES = (".services.ai.azure.com", ".cognitiveservices.azure.com")


def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def eljev_dir() -> Path:
    env_dir = _env_text("ELJEV_DIR")
    target = Path(env_dir).expanduser() if env_dir is not None else repo_root() / ".eljev"
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return target


def config_path() -> Path:
    return eljev_dir() / "config.json"


def load_config() -> dict[str, Any]:
    try:
        with config_path().open("r", encoding="utf-8") as stream:
            loaded = json.load(stream)
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_config(updates: Mapping[str, Any]) -> dict[str, Any]:
    merged = load_config()
    merged.update(dict(updates))
    target = config_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    temporary_path: str | None = None
    try:
        fd, temporary_path = tempfile.mkstemp(
            prefix=f"{target.name}.",
            suffix=".tmp",
            dir=str(target.parent),
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(merged, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
        os.replace(temporary_path, target)
        temporary_path = None
    finally:
        if temporary_path:
            try:
                os.remove(temporary_path)
            except OSError:
                pass
    return merged


def pidfile_path() -> Path:
    return eljev_dir() / "daemon.pid"


def log_dir() -> Path:
    env_dir = _env_text("ELJEV_LOG_DIR")
    return Path(env_dir).expanduser() if env_dir is not None else eljev_dir() / "logs"


def parse_switch(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in {"on", "1", "true", "yes", "enable", "enabled"}:
        return True
    if normalized in {"off", "0", "false", "no", "disable", "disabled"}:
        return False
    return None


def user_env_value(name: str) -> str | None:
    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
    except Exception:
        return None
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def enabled_state() -> tuple[bool, str]:
    for var_name, source in (
        ("EL_JEV", "env:EL_JEV"),
        ("ELJEV_ENABLED", "env:ELJEV_ENABLED"),
    ):
        parsed = parse_switch(_env_text(var_name))
        if parsed is not None:
            return parsed, source

    parsed = parse_switch(user_env_value("EL_JEV"))
    if parsed is not None:
        return parsed, "user-env:EL_JEV"

    parsed = parse_switch(load_config().get("enabled"))
    if parsed is not None:
        return parsed, "config"

    return True, "default"


def logging_enabled() -> bool:
    parsed = parse_switch(_env_text("ELJEV_LOGGING"))
    if parsed is not None:
        return parsed
    legacy = _env_text("ELJEV_NO_LOG")
    if legacy is not None and legacy.lower() in {"1", "true", "yes"}:
        return False
    return True


def cohere_endpoint() -> str:
    value = _env_text("ELJEV_COHERE_ENDPOINT")
    if value is None:
        configured = load_config().get("cohere_endpoint")
        value = configured if isinstance(configured, str) else ""
    return value.strip().rstrip("/")


def cohere_deployment() -> str:
    value = _env_text("ELJEV_COHERE_DEPLOYMENT")
    if value is not None:
        return value
    configured = load_config().get("cohere_deployment")
    if isinstance(configured, str):
        return configured
    return DEFAULT_DEPLOYMENT


def normalize_md1_endpoint(raw: str) -> str:
    text = str(raw).strip()
    parts = urlsplit(text)
    if parts.scheme.lower() != "https" or not parts.hostname:
        raise ValueError("md1 endpoint must use https://")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("md1 endpoint must not contain credentials, query, or fragment")
    host = parts.hostname.lower()
    if not host.endswith(ALLOWED_MD1_HOST_SUFFIXES):
        raise ValueError(
            "md1 endpoint host must end with .services.ai.azure.com or .cognitiveservices.azure.com"
        )
    try:
        parts.port
    except ValueError as exc:
        raise ValueError("md1 endpoint has an invalid port") from exc
    path = parts.path.rstrip("/")
    if path.lower() == MD1_PATH:
        path = ""
    if path:
        raise ValueError(f"unexpected md1 endpoint path: {parts.path}")
    return f"https://{parts.netloc}"


def engine(default: str = DEFAULT_ENGINE) -> str:
    value = _env_text("ELJEV_ENGINE")
    if value is None:
        configured = load_config().get("engine")
        value = configured if isinstance(configured, str) else None
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in VALID_ENGINES:
            return normalized
    return default if default in VALID_ENGINES else DEFAULT_ENGINE


def md1_endpoint_raw() -> str:
    value = _env_text("ELJEV_MD1_ENDPOINT")
    if value is None:
        configured = load_config().get("md1_endpoint")
        value = configured if isinstance(configured, str) else ""
    return value.strip().rstrip("/")


def md1_endpoint() -> str:
    value = md1_endpoint_raw()
    if not value:
        return ""
    try:
        return normalize_md1_endpoint(value)
    except ValueError:
        return value


def md1_deployment() -> str:
    value = _env_text("ELJEV_MD1_DEPLOYMENT")
    if value is not None:
        return value
    configured = load_config().get("md1_deployment")
    if isinstance(configured, str) and configured.strip():
        return configured
    return DEFAULT_MD1_DEPLOYMENT


def md1_timeout_ms() -> int:
    fallback = 2500
    for raw in (
        _env_text("ELJEV_MD1_TIMEOUT_MS"),
        _env_text("ELJEV_TIMEOUT_MS"),
    ):
        if raw is None:
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value > 0:
            return value
    configured = load_config().get("md1_timeout_ms")
    if isinstance(configured, bool):
        return fallback
    if isinstance(configured, int) and configured > 0:
        return configured
    if isinstance(configured, str):
        try:
            parsed = int(configured.strip())
        except ValueError:
            return fallback
        if parsed > 0:
            return parsed
    return fallback


def extension_mode() -> str:
    value = _env_text("ELJEV_EXTENSION_MODE")
    if value is None:
        configured = load_config().get("extension_mode")
        value = configured if isinstance(configured, str) else DEFAULT_EXTENSION_MODE
    mode = value.strip().lower()
    return mode if mode in VALID_EXTENSION_MODES else DEFAULT_EXTENSION_MODE


def coverage_policy() -> str:
    value = _env_text("ELJEV_COVERAGE_POLICY")
    if value is not None:
        return value
    configured = load_config().get("coverage_policy")
    if isinstance(configured, str):
        return configured
    return DEFAULT_POLICY


def auth_mode() -> str:
    value = _env_text("ELJEV_AUTH")
    if value is None:
        configured = load_config().get("auth")
        value = configured if isinstance(configured, str) else "azcli"
    mode = value.strip().lower()
    return mode if mode in VALID_AUTH else "azcli"


def azure_subscription() -> str:
    """Subscription whose signed-in az account mints tokens; empty means the az default."""

    value = _env_text("ELJEV_AZURE_SUBSCRIPTION")
    if value is None:
        configured = load_config().get("subscription")
        value = configured if isinstance(configured, str) else ""
    return value.strip()


def hook_mode() -> str:
    value = _env_text("ELJEV_HOOK_MODE")
    if value is None:
        configured = load_config().get("hook_mode")
        value = configured if isinstance(configured, str) else "intent"
    mode = value.strip().lower()
    return mode if mode in VALID_HOOK_MODES else "intent"


def hook_timeout_ms() -> int:
    raw = _env_text("ELJEV_HOOK_TIMEOUT_MS")
    if raw is not None:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    configured = load_config().get("hook_timeout_ms")
    if isinstance(configured, int) and configured > 0:
        return configured
    return DEFAULT_HOOK_TIMEOUT_MS


def calibration_path() -> Path:
    env_path = _env_text("ELJEV_CALIBRATION_PATH")
    if env_path is not None:
        return Path(env_path).expanduser()
    return repo_root() / "eval" / "calibration.json"


def _env_text(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None
