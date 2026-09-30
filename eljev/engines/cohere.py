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
import shutil
import socket
import ssl
import subprocess
import threading
import time
from typing import Any
from urllib.parse import urlsplit


COHERE_PATH = "/providers/cohere/v2/rerank"
DEFAULT_DEPLOYMENT = "Cohere-rerank-v4.0-pro"
DEFAULT_TIMEOUT_MS = 2500
TOKEN_SCOPE = "https://ai.azure.com/.default"


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
        self.endpoint = endpoint or os.environ.get("ELJEV_COHERE_ENDPOINT", "")
        self.deployment = deployment or os.environ.get(
            "ELJEV_COHERE_DEPLOYMENT", DEFAULT_DEPLOYMENT
        )
        self.timeout_ms = int(
            timeout_ms if timeout_ms is not None else os.environ.get("ELJEV_TIMEOUT_MS", DEFAULT_TIMEOUT_MS)
        )
        self.token_timeout_s = token_timeout_s
        self._connection_factory = connection_factory or http.client.HTTPSConnection
        self._token_provider = token_provider
        self._connection: Any | None = None
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._lock = threading.Lock()

    @staticmethod
    def parse_response(payload: Any) -> dict[str, Any]:
        return parse_cohere_response(payload)

    def close(self) -> None:
        with self._lock:
            self._close_connection()

    def _close_connection(self) -> None:
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

    def _get_connection(self, timeout_s: float) -> Any:
        host, port = self._endpoint_parts()
        if self._connection is None:
            self._connection = self._connection_factory(host, port=port, timeout=timeout_s)
        else:
            try:
                self._connection.timeout = timeout_s
            except (AttributeError, OSError):
                pass
        return self._connection

    def _mint_token(self) -> str:
        if self._token_provider is not None:
            token = self._token_provider()
            if not isinstance(token, str) or not token.strip():
                raise CohereError("auth", "Azure token provider returned an empty token")
            return token.strip()
        try:
            az_executable = shutil.which("az") or shutil.which("az.cmd") or "az"
            completed = subprocess.run(
                [
                    az_executable,
                    "account",
                    "get-access-token",
                    "--scope",
                    TOKEN_SCOPE,
                    "--query",
                    "accessToken",
                    "-o",
                    "tsv",
                ],
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
        token = completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else ""
        if "\t" in token:
            token = token.split("\t")[0].strip()
        if not token:
            raise CohereError("auth", "Azure CLI returned an empty token")
        return token

    def _get_token(self) -> str:
        now = time.monotonic()
        if self._token and self._token_expires_at > now + 5.0:
            return self._token
        token = self._mint_token()
        self._token = token
        self._token_expires_at = now + 55 * 60
        return token

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
        ).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        attempt = 0
        with self._lock:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CohereError("timeout", "Cohere inference deadline exceeded")
                try:
                    connection = self._get_connection(remaining)
                    connection.request("POST", COHERE_PATH, body=body, headers=headers)
                    response = connection.getresponse()
                    response_body = response.read()
                except (OSError, http.client.HTTPException) as exc:
                    self._close_connection()
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

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CohereError("timeout", "Cohere inference deadline exceeded")
                attempt += 1
                retry_after = self._retry_after(response.getheader("Retry-After"))
                backoff = retry_after if retry_after is not None else min(
                    1.0, 0.1 * (2 ** (attempt - 1)) + random.uniform(0.0, 0.1)
                )
                time.sleep(min(backoff, remaining))
