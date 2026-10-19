"""Shared Azure Foundry bearer token provider."""

from __future__ import annotations

import json
import math
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .. import config


TOKEN_SCOPE = "https://ai.azure.com/.default"
TOKEN_REFRESH_SKEW_S = 300.0
FALLBACK_TOKEN_TTL_S = 55 * 60.0
AUTH_FAIL_COOLDOWN_S = 300.0
_AUTH_FAIL_COOLDOWN_S = AUTH_FAIL_COOLDOWN_S
AZCLI_NONINTERACTIVE_ENV: dict[str, str] = {
    "AZURE_CORE_NO_COLOR": "true",
    "AZURE_CORE_ONLY_SHOW_ERRORS": "true",
    "AZURE_CORE_DISABLE_CONFIRM_PROMPT": "true",
    "GCM_INTERACTIVE": "never",
}
_AZCLI_CHILD_EXES = frozenset({"cmd.exe", "python.exe", "python3.exe", "az.exe", "conhost.exe"})


def build_azcli_env() -> dict[str, str]:
    """Return an environment dictionary that suppresses interactive Azure CLI prompts."""

    env = dict(os.environ)
    env.update(AZCLI_NONINTERACTIVE_ENV)
    return env


def azcli_creationflags() -> int:
    """Return Windows creation flags that hide console windows and isolate process groups."""

    if sys.platform != "win32":
        return 0
    return (
        getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    )


def _snapshot_windows_processes() -> dict[int, tuple[int, str]]:
    if sys.platform != "win32":
        return {}
    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32FirstW.restype = wintypes.BOOL
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == INVALID_HANDLE_VALUE:
            return {}
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            processes: dict[int, tuple[int, str]] = {}
            if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
                return {}
            while True:
                pid = int(entry.th32ProcessID)
                ppid = int(entry.th32ParentProcessID)
                exe = str(entry.szExeFile).strip().lower()
                processes[pid] = (ppid, exe)
                if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                    break
            return processes
        finally:
            kernel32.CloseHandle(snapshot)
    except Exception:
        return {}


def _find_spawned_azcli_pids(
    before_pids: set[int],
    after_processes: Mapping[int, tuple[int, str]],
    *,
    owner_pid: int | None = None,
) -> list[int]:
    current_pid = os.getpid() if owner_pid is None else int(owner_pid)
    new_pids = {pid for pid in after_processes if pid not in before_pids and pid != current_pid}
    if not new_pids:
        return []

    to_terminate: list[int] = []
    for pid in sorted(new_pids):
        ppid, exe = after_processes[pid]
        if exe not in _AZCLI_CHILD_EXES:
            continue
        cursor_ppid = ppid
        visited: set[int] = {pid}
        owned_or_orphaned = False
        while True:
            if cursor_ppid == current_pid:
                owned_or_orphaned = True
                break
            if cursor_ppid in before_pids or cursor_ppid <= 0 or cursor_ppid in visited:
                break
            visited.add(cursor_ppid)
            parent_info = after_processes.get(cursor_ppid)
            if parent_info is None:
                owned_or_orphaned = True
                break
            cursor_ppid = parent_info[0]
        if owned_or_orphaned:
            to_terminate.append(pid)
    return to_terminate


def _terminate_spawned_azcli_tree(before_pids: set[int]) -> list[int]:
    if sys.platform != "win32":
        return []
    after_processes = _snapshot_windows_processes()
    target_pids = _find_spawned_azcli_pids(before_pids, after_processes)
    terminated: list[int] = []
    for pid in target_pids:
        try:
            os.kill(pid, signal.SIGTERM)
            terminated.append(pid)
        except OSError:
            continue
    return terminated


def run_azcli_subprocess(
    command: list[str],
    *,
    timeout_s: float,
    run_fn: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    """Run an Azure CLI command non-interactively and clean up orphaned children on timeout."""

    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "shell": False,
        "check": False,
        "capture_output": True,
        "text": True,
        "timeout": timeout_s,
        "env": build_azcli_env(),
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = azcli_creationflags()

    is_real_subprocess = run_fn is subprocess.run
    before_pids = (
        set(_snapshot_windows_processes().keys())
        if (sys.platform == "win32" and is_real_subprocess)
        else set()
    )
    watchdog: threading.Timer | None = None
    if sys.platform == "win32" and is_real_subprocess:
        watchdog = threading.Timer(
            max(0.1, float(timeout_s)) + 0.5,
            _terminate_spawned_azcli_tree,
            args=(before_pids,),
        )
        watchdog.daemon = True
        watchdog.start()
    try:
        return run_fn(command, **kwargs)
    except subprocess.TimeoutExpired:
        if sys.platform == "win32":
            _terminate_spawned_azcli_tree(before_pids)
        raise
    finally:
        if watchdog is not None:
            watchdog.cancel()


class FoundryAuthError(RuntimeError):
    """A safe authentication error that never includes credentials."""

    def __init__(self, error_kind: str, message: str) -> None:
        super().__init__(message)
        self.error_kind = error_kind


class FoundryBearerProvider:
    """Mint and cache Azure Foundry bearer tokens for multiple engines."""

    def __init__(
        self,
        *,
        mode: str | None = None,
        scope: str = TOKEN_SCOPE,
        token_timeout_s: float = 15.0,
        refresh_skew_s: float = TOKEN_REFRESH_SKEW_S,
        fallback_ttl_s: float = FALLBACK_TOKEN_TTL_S,
        failure_cooldown_s: float = AUTH_FAIL_COOLDOWN_S,
    ) -> None:
        self._mode = mode.strip().lower() if isinstance(mode, str) else None
        self.scope = scope
        self.token_timeout_s = float(token_timeout_s)
        self.refresh_skew_s = float(refresh_skew_s)
        self.fallback_ttl_s = float(fallback_ttl_s)
        self.failure_cooldown_s = max(0.0, float(failure_cooldown_s))
        self._lock = threading.Lock()
        self._token: str | None = None
        self._token_expiry_epoch = 0.0
        self._failure_until_epoch = 0.0
        self._last_error: FoundryAuthError | None = None

    @staticmethod
    def _parse_expiry_epoch(payload: Mapping[str, Any]) -> float | None:
        value = payload.get("expires_on")
        if value is None:
            value = payload.get("expiresOn")
        if value is None:
            return None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            epoch = float(value)
            return epoch if math.isfinite(epoch) and epoch > 0 else None
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            if text.isdigit():
                epoch = float(int(text))
                return epoch if epoch > 0 else None
            normalized = text.replace("Z", "+00:00")
            try:
                parsed = datetime.fromisoformat(normalized)
            except ValueError:
                try:
                    parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f")
                except ValueError:
                    return None
            return parsed.timestamp()
        return None

    def _effective_mode(self) -> str:
        mode = self._mode or config.auth_mode()
        normalized = mode.strip().lower()
        return "managed_identity" if normalized == "managed_identity" else "azcli"

    def _mint_azcli_token(self) -> tuple[str, float | None]:
        command = [
            shutil.which("az") or "az",
            "account",
            "get-access-token",
            "--scope",
            self.scope,
            "-o",
            "json",
        ]
        subscription = config.azure_subscription()
        if subscription:
            command[3:3] = ["--subscription", subscription]
        try:
            completed = run_azcli_subprocess(
                command,
                timeout_s=self.token_timeout_s,
                run_fn=subprocess.run,
            )
        except subprocess.TimeoutExpired as exc:
            raise FoundryAuthError("timeout", "Azure token minting timed out") from exc
        except OSError as exc:
            raise FoundryAuthError("auth", "Azure CLI token minting failed") from exc
        if completed.returncode != 0:
            raise FoundryAuthError("auth", "Azure CLI token minting failed")
        try:
            payload = json.loads(completed.stdout)
        except (TypeError, ValueError) as exc:
            raise FoundryAuthError("auth", "Azure CLI token minting failed") from exc
        if not isinstance(payload, Mapping):
            raise FoundryAuthError("auth", "Azure CLI token minting failed")
        token_raw = payload.get("accessToken")
        if not isinstance(token_raw, str):
            raise FoundryAuthError("auth", "Azure CLI token minting failed")
        token = token_raw.strip()
        if "\t" in token:
            token = token.split("\t", maxsplit=1)[0].strip()
        if not token:
            raise FoundryAuthError("auth", "Azure CLI returned an empty token")
        return token, self._parse_expiry_epoch(payload)

    def _mint_managed_identity_token(self) -> tuple[str, float | None]:
        identity_endpoint = os.environ.get("IDENTITY_ENDPOINT", "").strip()
        identity_header = os.environ.get("IDENTITY_HEADER", "").strip()
        client_id = os.environ.get("AZURE_CLIENT_ID", "").strip()
        if identity_endpoint and identity_header:
            query = {
                "resource": "https://ai.azure.com",
                "api-version": "2019-08-01",
            }
            if client_id:
                query["client_id"] = client_id
            token_url = f"{identity_endpoint}?{urlencode(query)}"
            headers = {"X-IDENTITY-HEADER": identity_header}
        else:
            token_url = (
                "http://169.254.169.254/metadata/identity/oauth2/token"
                "?api-version=2018-02-01&resource=https://ai.azure.com"
            )
            if client_id:
                token_url += f"&client_id={quote(client_id)}"
            headers = {"Metadata": "true"}
        request = Request(token_url, method="GET", headers=headers)
        try:
            with urlopen(request, timeout=self.token_timeout_s) as response:
                payload = json.loads(response.read())
        except TimeoutError as exc:
            raise FoundryAuthError("auth", "Managed identity token minting failed") from exc
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise FoundryAuthError("auth", "Managed identity token minting failed") from exc
        if not isinstance(payload, Mapping):
            raise FoundryAuthError("auth", "Managed identity token minting failed")
        token_raw = payload.get("access_token") or payload.get("accessToken")
        if not isinstance(token_raw, str) or not token_raw.strip():
            raise FoundryAuthError("auth", "Managed identity token minting failed")
        return token_raw.strip(), self._parse_expiry_epoch(payload)

    def _mint_token(self) -> tuple[str, float]:
        if self._effective_mode() == "managed_identity":
            token, expiry_epoch = self._mint_managed_identity_token()
        else:
            token, expiry_epoch = self._mint_azcli_token()
        expiry = expiry_epoch if expiry_epoch is not None else time.time() + self.fallback_ttl_s
        return token, float(expiry)

    def _token_valid(self, now_epoch: float) -> bool:
        return (
            isinstance(self._token, str)
            and bool(self._token)
            and self._token_expiry_epoch > (now_epoch + self.refresh_skew_s)
        )

    def _raise_cooldown_error(self) -> None:
        err = self._last_error
        if err is not None:
            raise FoundryAuthError(err.error_kind, str(err))
        raise FoundryAuthError("auth", "Azure token minting is in failure cooldown")

    def get_token(self) -> str:
        now_epoch = time.time()
        if self._token_valid(now_epoch):
            return str(self._token)
        if now_epoch < self._failure_until_epoch:
            if isinstance(self._token, str) and self._token and self._token_expiry_epoch > now_epoch:
                return self._token
            self._raise_cooldown_error()
        if not self._lock.acquire(blocking=False):
            if isinstance(self._token, str) and self._token and self._token_expiry_epoch > now_epoch:
                return self._token
            self._lock.acquire()
        try:
            now_epoch = time.time()
            if self._token_valid(now_epoch):
                return str(self._token)
            if now_epoch < self._failure_until_epoch:
                if isinstance(self._token, str) and self._token and self._token_expiry_epoch > now_epoch:
                    return self._token
                self._raise_cooldown_error()
            try:
                token, expiry_epoch = self._mint_token()
            except FoundryAuthError as exc:
                self._last_error = exc
                self._failure_until_epoch = now_epoch + self.failure_cooldown_s
                if isinstance(self._token, str) and self._token and self._token_expiry_epoch > now_epoch:
                    return self._token
                raise
            self._token = token
            self._token_expiry_epoch = float(expiry_epoch)
            self._failure_until_epoch = 0.0
            self._last_error = None
            return token
        finally:
            self._lock.release()

    def warm(self) -> bool:
        try:
            self.get_token()
        except FoundryAuthError:
            return False
        return True
