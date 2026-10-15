"""Shared Azure Foundry bearer token provider."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import json
import math
import os
import shutil
import subprocess
import threading
import time
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from .. import config


TOKEN_SCOPE = "https://ai.azure.com/.default"
TOKEN_REFRESH_SKEW_S = 300.0
FALLBACK_TOKEN_TTL_S = 55 * 60.0


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
    ) -> None:
        self._mode = mode.strip().lower() if isinstance(mode, str) else None
        self.scope = scope
        self.token_timeout_s = float(token_timeout_s)
        self.refresh_skew_s = float(refresh_skew_s)
        self.fallback_ttl_s = float(fallback_ttl_s)
        self._lock = threading.Lock()
        self._token: str | None = None
        self._token_expiry_epoch = 0.0

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
            completed = subprocess.run(
                command,
                shell=False,
                check=False,
                capture_output=True,
                text=True,
                timeout=self.token_timeout_s,
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

    def get_token(self) -> str:
        now_epoch = time.time()
        if self._token_valid(now_epoch):
            return str(self._token)
        if not self._lock.acquire(blocking=False):
            if isinstance(self._token, str) and self._token and self._token_expiry_epoch > now_epoch:
                return self._token
            self._lock.acquire()
        try:
            now_epoch = time.time()
            if self._token_valid(now_epoch):
                return str(self._token)
            token, expiry_epoch = self._mint_token()
            self._token = token
            self._token_expiry_epoch = float(expiry_epoch)
            return token
        finally:
            self._lock.release()

    def warm(self) -> bool:
        try:
            self.get_token()
        except FoundryAuthError:
            return False
        return True
