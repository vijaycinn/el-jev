"""Cohere rerank client for the verified Azure AI Foundry route."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
import email.utils
import http.client
import json
import math
import os
import random
import select
import shutil
import socket
import ssl
import subprocess
import threading
import time
from typing import Any
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from .. import config


COHERE_PATH = "/providers/cohere/v2/rerank"
DEFAULT_DEPLOYMENT = "Cohere-rerank-v4.0-pro"
DEFAULT_TIMEOUT_MS = 2500
TOKEN_SCOPE = "https://ai.azure.com/.default"
TOKEN_REFRESH_SKEW_S = 300.0
FALLBACK_TOKEN_TTL_S = 55 * 60.0
MAX_HTTP_ATTEMPTS = 4
STALE_REUSE_ERRORS = (
    http.client.RemoteDisconnected,
    ConnectionResetError,
    BrokenPipeError,
    ConnectionAbortedError,
)


class CohereError(RuntimeError):
    """A safe provider error that never contains credentials."""

    def __init__(self, error_kind: str, message: str) -> None:
        super().__init__(message)
        self.error_kind = error_kind


def parse_cohere_response(payload: Any) -> dict[str, Any]:
    """Decode a Cohere response or the score-list fixture used by unit tests."""

    if isinstance(payload, (bytes, bytearray, str)):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError) as exc:
            raise CohereError("malformed", "Cohere response was not valid JSON") from exc

    if isinstance(payload, list):
        results = []
        for index, score in enumerate(payload):
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise CohereError("malformed", "Cohere score fixture contained a non-numeric value")
            if not math.isfinite(float(score)):
                raise CohereError("malformed", "Cohere score fixture contained a non-finite value")
            results.append({"index": index, "relevance_score": float(score)})
        return {"results": results}

    if not isinstance(payload, Mapping):
        raise CohereError("malformed", "Cohere response must be a JSON object")
    results = payload.get("results")
    if not isinstance(results, list):
        raise CohereError("malformed", "Cohere response did not contain results")
    return dict(payload)


parse_response = parse_cohere_response


class CohereClient:
    """A connection-reusing, deadline-bounded Cohere rerank client."""

    def __init__(
        self,
        endpoint: str | None = None,
        *,
        deployment: str | None = None,
        timeout_ms: int | None = None,
        token_timeout_s: float = 15.0,
        connection_factory: Callable[..., Any] | None = None,
        token_provider: Callable[[], str] | None = None,
    ) -> None:
        self.endpoint = config.cohere_endpoint() if endpoint is None else endpoint
        self.deployment = config.cohere_deployment() if deployment is None else deployment
        self.timeout_ms = int(
            timeout_ms if timeout_ms is not None else os.environ.get("ELJEV_TIMEOUT_MS", DEFAULT_TIMEOUT_MS)
        )
        self.token_timeout_s = float(token_timeout_s)
        self._connection_factory = connection_factory or http.client.HTTPSConnection
        self._token_provider = token_provider
        self._connection: Any | None = None
        self._connection_lock = threading.Lock()
        self._token_lock = threading.Lock()
        self._token: str | None = None
        self._token_expiry_epoch = 0.0

    @staticmethod
    def parse_response(payload: Any) -> dict[str, Any]:
        return parse_cohere_response(payload)

    def close(self) -> None:
        with self._connection_lock:
            self._close_connection_locked()

    def _close_connection_locked(self) -> None:
        connection = self._connection
        self._connection = None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

    def _endpoint_parts(self) -> tuple[str, int | None]:
        if not self.endpoint:
            raise CohereError("http", "Cohere endpoint is not configured")
        parsed = urlsplit(self.endpoint)
        if parsed.scheme.lower() != "https" or not parsed.hostname:
            raise CohereError("tls", "Cohere endpoint must use HTTPS")
        try:
            port = parsed.port
        except ValueError as exc:
            raise CohereError("connection", "Cohere endpoint has an invalid port") from exc
        return parsed.hostname, port

    def _get_connection_locked(self, timeout_s: float) -> Any:
        host, port = self._endpoint_parts()
        if self._connection is None:
            self._connection = self._connection_factory(host, port=port, timeout=timeout_s)
        else:
            try:
                self._connection.timeout = timeout_s
            except (AttributeError, OSError):
                pass
        return self._connection

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
            # az CLI reports `expiresOn` as naive local time; naive timestamp() is local.
            return parsed.timestamp()
        return None

    def _mint_azcli_token(self) -> tuple[str, float | None]:
        command = [
            shutil.which("az") or "az",
            "account",
            "get-access-token",
            "--scope",
            TOKEN_SCOPE,
            "-o",
            "json",
        ]
        subscription = config.azure_subscription()
        if subscription:
            # Mint with the account that owns the resource's subscription so a different
            # az default account cannot produce "Token tenant ... does not match resource tenant".
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
            raise CohereError("timeout", "Azure token minting timed out") from exc
        except OSError as exc:
            raise CohereError("auth", "Azure CLI token minting failed") from exc
        if completed.returncode != 0:
            raise CohereError("auth", "Azure CLI token minting failed")
        try:
            payload = json.loads(completed.stdout)
        except (TypeError, ValueError) as exc:
            raise CohereError("auth", "Azure CLI token minting failed") from exc
        if not isinstance(payload, Mapping):
            raise CohereError("auth", "Azure CLI token minting failed")
        token_raw = payload.get("accessToken")
        if not isinstance(token_raw, str):
            raise CohereError("auth", "Azure CLI token minting failed")
        token = token_raw.strip()
        if "\t" in token:
            token = token.split("\t")[0].strip()
        if not token:
            raise CohereError("auth", "Azure CLI returned an empty token")
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
            raise CohereError("auth", "Managed identity token minting failed") from exc
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise CohereError("auth", "Managed identity token minting failed") from exc
        if not isinstance(payload, Mapping):
            raise CohereError("auth", "Managed identity token minting failed")
        token_raw = payload.get("access_token") or payload.get("accessToken")
        if not isinstance(token_raw, str) or not token_raw.strip():
            raise CohereError("auth", "Managed identity token minting failed")
        token = token_raw.strip()
        return token, self._parse_expiry_epoch(payload)

    def _mint_token(self) -> tuple[str, float]:
        if self._token_provider is not None:
            token = self._token_provider()
            if not isinstance(token, str) or not token.strip():
                raise CohereError("auth", "Azure token provider returned an empty token")
            return token.strip(), time.time() + FALLBACK_TOKEN_TTL_S
        mode = config.auth_mode()
        if mode == "managed_identity":
            token, expiry_epoch = self._mint_managed_identity_token()
        else:
            token, expiry_epoch = self._mint_azcli_token()
        expiry = expiry_epoch if expiry_epoch is not None else time.time() + FALLBACK_TOKEN_TTL_S
        return token, expiry

    def _token_valid(self, now_epoch: float) -> bool:
        return (
            isinstance(self._token, str)
            and bool(self._token)
            and self._token_expiry_epoch > (now_epoch + TOKEN_REFRESH_SKEW_S)
        )

    def _get_token(self) -> str:
        now_epoch = time.time()
        if self._token_valid(now_epoch):
            return str(self._token)
        if not self._token_lock.acquire(blocking=False):
            # Another thread is refreshing; serve the still-unexpired token instead of
            # blocking a request behind `az` (up to token_timeout_s).
            if isinstance(self._token, str) and self._token and self._token_expiry_epoch > now_epoch:
                return self._token
            self._token_lock.acquire()
        try:
            now_epoch = time.time()
            if self._token_valid(now_epoch):
                return str(self._token)
            token, expiry_epoch = self._mint_token()
            self._token = token
            self._token_expiry_epoch = float(expiry_epoch)
            return token
        finally:
            self._token_lock.release()

    @staticmethod
    def _retry_after(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None

    @staticmethod
    def _http_error(status: int) -> CohereError:
        if status in (401, 403):
            return CohereError("auth", "Cohere authentication failed")
        if status == 408:
            return CohereError("timeout", "Cohere request timed out")
        if status == 429:
            return CohereError("rate_limited", "Cohere rate limit was exceeded")
        return CohereError("http", f"Cohere returned HTTP {status}")

    @staticmethod
    def _transport_error(exc: BaseException) -> CohereError:
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return CohereError("timeout", "Cohere request timed out")
        if isinstance(exc, ssl.SSLError):
            return CohereError("tls", "Cohere TLS connection failed")
        if isinstance(exc, socket.gaierror):
            return CohereError("dns", "Cohere hostname could not be resolved")
        return CohereError("connection", "Cohere connection failed")

    @staticmethod
    def _idle_connection_is_stale(connection: Any) -> bool:
        """An idle keep-alive socket should never be readable; readable means the server closed it."""

        sock = getattr(connection, "sock", None)
        if sock is None or not hasattr(sock, "fileno"):
            return False
        try:
            if sock.fileno() < 0:
                return True
            readable, _, _ = select.select([sock], [], [], 0)
        except (OSError, ValueError):
            return True
        return bool(readable)

    def warm(self) -> bool:
        """Warm token + TLS connection. Returns False on any failure."""

        if not self.endpoint:
            return False
        try:
            self._get_token()
            timeout = max(0.1, self.timeout_ms / 1000.0)
            # Never make an in-flight rerank wait on a warm-up handshake.
            if not self._connection_lock.acquire(blocking=False):
                return True
            try:
                connection = self._get_connection_locked(timeout)
                if self._idle_connection_is_stale(connection):
                    # Reconnect now so the next request does not pay reconnect + retry.
                    self._close_connection_locked()
                    connection = self._get_connection_locked(timeout)
                if getattr(connection, "sock", None) is None:
                    connection.connect()
            finally:
                self._connection_lock.release()
            return True
        except Exception:
            with self._connection_lock:
                self._close_connection_locked()
            return False

    def rerank(self, criterion: str, documents: list[str], top_n: int) -> dict[str, Any]:
        """Run one rerank call with one total inference deadline."""

        token = self._get_token()
        deadline = time.monotonic() + max(0.001, self.timeout_ms / 1000.0)
        body = json.dumps(
            {
                "model": self.deployment,
                "query": criterion,
                "documents": documents,
                "top_n": top_n,
                "return_documents": False,
            },
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        attempt = 0
        stale_retry_used = False
        while True:
            backoff: float | None = None
            with self._connection_lock:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CohereError("timeout", "Cohere inference deadline exceeded")
                reused_connection = self._connection is not None
                try:
                    connection = self._get_connection_locked(remaining)
                    connection.request("POST", COHERE_PATH, body=body, headers=headers)
                    response = connection.getresponse()
                except STALE_REUSE_ERRORS as exc:
                    # An idle keep-alive that Azure already closed fails before any
                    # response bytes arrive; retry once on a fresh connection. A fresh
                    # connection is never retried, so a processed request is billed at most twice.
                    self._close_connection_locked()
                    if reused_connection and not stale_retry_used:
                        stale_retry_used = True
                        continue
                    raise self._transport_error(exc) from exc
                except (OSError, http.client.HTTPException) as exc:
                    self._close_connection_locked()
                    raise self._transport_error(exc) from exc

                try:
                    response_body = response.read()
                except (OSError, http.client.HTTPException) as exc:
                    self._close_connection_locked()
                    raise self._transport_error(exc) from exc

                status = int(response.status)
                if 200 <= status < 300:
                    try:
                        return parse_cohere_response(response_body)
                    except CohereError:
                        raise
                    except (TypeError, ValueError) as exc:
                        raise CohereError("malformed", "Cohere response could not be parsed") from exc

                retryable = status in (408, 409, 429) or status >= 500
                if not retryable:
                    raise self._http_error(status)

                attempt += 1
                if attempt >= MAX_HTTP_ATTEMPTS:
                    raise self._http_error(status)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CohereError("timeout", "Cohere inference deadline exceeded")
                retry_after = self._retry_after(response.getheader("Retry-After"))
                backoff = min(
                    remaining,
                    retry_after
                    if retry_after is not None
                    else min(1.0, 0.1 * (2 ** (attempt - 1)) + random.uniform(0.0, 0.1)),
                )
            # Back off without holding the connection lock so other requests are not stalled.
            time.sleep(backoff)
