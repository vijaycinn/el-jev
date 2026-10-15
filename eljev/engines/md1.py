"""Microsoft Decision-1 engine client and Shape A adapter."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import email.utils
import http.client
import json
import math
import random
import select
import socket
import ssl
import threading
import time
from typing import Any
from urllib.parse import urlsplit

from .. import config
from ..pregate import check_pregate
from ..types import Candidate, DecisionRecord
from ..verdict import apply_gate, calibration_for, load_calibration
from .auth import FoundryAuthError, FoundryBearerProvider
from .systemone import (
    SystemOneError,
    _normalize_choice_options,
    _normalize_instructions,
    _normalize_noul_candidates,
    _normalize_score_levels,
    _serialize_state,
)


MD1_PATH = "/providers/microsoft/v1/systemone"
MAX_HTTP_ATTEMPTS = 4
STALE_REUSE_ERRORS = (
    http.client.RemoteDisconnected,
    ConnectionResetError,
    BrokenPipeError,
    ConnectionAbortedError,
)

INTENT_CRITERIA = {
    "code_modification": "Implement, write, edit, generate, refactor, or fix code in files",
    "review_audit": "Perform code review, security review, PR critique, or safety audit",
    "investigation_search": "Search codebase, find symbols/files, explore architecture, check logs or documentation",
    "execution_testing": "Run terminal commands, builds, test suites, or deployment tasks",
    "advisory_explanation": "Provide high-level architecture advice, conceptual explanations, or technical explanations",
}

DEEP_ANALYSIS_CRITERIA = {
    "true": "Ambiguous scope, multi-intent, architectural trade-offs, or complex multi-file design requiring deliberation",
    "false": "Single clear bounded intent with direct local lookup, command, or edit",
}

EXECUTION_TIER_CRITERIA = {
    "fast_direct": "Direct answer or 1-3 direct tool calls (grep/view/edit) with zero sub-agents and no upfront plan prose",
    "bounded_work": "Standard single-track execution in <=5 direct tool calls without sub-agents",
    "deep_system2": "Complex multi-step work where planning or specialist sub-agent delegation is warranted",
}


class MD1Error(RuntimeError):
    def __init__(self, error_kind: str, message: str) -> None:
        super().__init__(message)
        self.error_kind = error_kind


def _finite_unit_interval(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and 0.0 <= float(value) <= 1.0
    )


def _finite_non_bool(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def validate_md1_response(
    payload: Any,
    questions_spec: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate the MD1 wire response against question specs."""

    if isinstance(payload, (bytes, bytearray, str)):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return None, "MD1 response was not valid JSON"

    if not isinstance(payload, Mapping):
        return None, "MD1 response must be an object"

    answers = payload.get("answers")
    if not isinstance(answers, Mapping):
        return None, "MD1 response must contain an answers object"

    if set(answers.keys()) != set(questions_spec.keys()):
        return None, "MD1 response answers did not match requested question ids"

    normalized_answers: dict[str, dict[str, Any]] = {}
    for qid, qspec in questions_spec.items():
        answer = answers.get(qid)
        if not isinstance(answer, Mapping):
            return None, f"answer {qid!r} must be an object"

        qtype = qspec.get("type")
        if qtype == "choice":
            criteria = qspec.get("criteria")
            if not isinstance(criteria, Mapping):
                return None, f"question {qid!r} criteria must be an object"
            expected_keys = {str(key) for key in criteria.keys()}
            probabilities = answer.get("probabilities")
            if not isinstance(probabilities, Mapping):
                return None, f"choice answer {qid!r} probabilities must be an object"
            if set(probabilities.keys()) != expected_keys:
                return None, f"choice answer {qid!r} probabilities keys did not match criteria keys"
            parsed_probabilities: dict[str, float] = {}
            for option_id, probability in probabilities.items():
                if not _finite_unit_interval(probability):
                    return (
                        None,
                        f"choice answer {qid!r} probability for {option_id!r} must be finite and in [0, 1]",
                    )
                parsed_probabilities[str(option_id)] = float(probability)
            if abs(sum(parsed_probabilities.values()) - 1.0) > 1e-4:
                return None, f"choice answer {qid!r} probabilities must sum to 1.0"
            confidence = answer.get("confidence")
            if not _finite_unit_interval(confidence):
                return None, f"choice answer {qid!r} confidence must be finite and in [0, 1]"
            choice_value = answer.get("choice")
            if not isinstance(choice_value, str) or choice_value not in expected_keys:
                return None, f"choice answer {qid!r} must include a valid choice"
            normalized_answers[qid] = {
                "choice": choice_value,
                "confidence": float(confidence),
                "probabilities": parsed_probabilities,
            }
            continue

        if qtype == "noul":
            noul_value = answer.get("noul")
            if not _finite_unit_interval(noul_value):
                return None, f"noul answer {qid!r} must be finite and in [0, 1]"
            normalized_answers[qid] = {"noul": float(noul_value)}
            continue

        if qtype == "score":
            levels = qspec.get("criteria")
            if not isinstance(levels, Sequence) or isinstance(levels, (str, bytes, bytearray)):
                return None, f"score question {qid!r} criteria must be a list"
            expected_keys = {str(index) for index in range(len(levels))}
            probabilities = answer.get("probabilities")
            if not isinstance(probabilities, Mapping):
                return None, f"score answer {qid!r} probabilities must be an object"
            if set(probabilities.keys()) != expected_keys:
                return None, f"score answer {qid!r} probabilities keys did not match score levels"
            parsed_probabilities: dict[str, float] = {}
            for level_id, probability in probabilities.items():
                if not _finite_unit_interval(probability):
                    return (
                        None,
                        f"score answer {qid!r} probability for {level_id!r} must be finite and in [0, 1]",
                    )
                parsed_probabilities[str(level_id)] = float(probability)
            if abs(sum(parsed_probabilities.values()) - 1.0) > 1e-4:
                return None, f"score answer {qid!r} probabilities must sum to 1.0"
            score_value = answer.get("score")
            if not _finite_non_bool(score_value) or not 0.0 <= float(score_value) <= float(len(levels) - 1):
                return (
                    None,
                    f"score answer {qid!r} score must be finite and in [0, {float(len(levels) - 1):g}]",
                )
            confidence = answer.get("confidence")
            normalized = {
                "score": float(score_value),
                "probabilities": parsed_probabilities,
            }
            if confidence is not None:
                if not _finite_unit_interval(confidence):
                    return None, f"score answer {qid!r} confidence must be finite and in [0, 1]"
                normalized["confidence"] = float(confidence)
            normalized_answers[qid] = normalized
            continue

        return None, f"unsupported question type {qtype!r} for {qid!r}"

    return {"answers": normalized_answers}, None


def _power_scale_probabilities(probs: list[float], temperature: float) -> list[float]:
    if abs(temperature - 1.0) <= 1e-9:
        return [float(probability) for probability in probs]
    safe_t = max(1e-4, float(temperature))
    logits = [math.log(max(float(probability), 1e-12)) / safe_t for probability in probs]
    max_logit = max(logits)
    exp_values = [math.exp(value - max_logit) for value in logits]
    exp_sum = sum(exp_values)
    if exp_sum <= 0.0:
        return [1.0 / len(exp_values)] * len(exp_values)
    return [value / exp_sum for value in exp_values]


def _elapsed_ms(start: float, engine_start: float, engine_end: float) -> dict[str, float]:
    total_ms = (time.perf_counter() - start) * 1000.0
    engine_ms = max(0.0, (engine_end - engine_start) * 1000.0)
    return {
        "total": round(total_ms, 3),
        "pregate": 0.0,
        "engine": round(engine_ms, 3),
        "overhead": round(max(0.0, total_ms - engine_ms), 3),
    }


class MD1Client:
    """Connection-reusing MD1 HTTP client."""

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
        self.endpoint = config.md1_endpoint() if endpoint is None else str(endpoint).strip().rstrip("/")
        self.deployment = config.md1_deployment() if deployment is None else str(deployment)
        configured_timeout = config.md1_timeout_ms() if timeout_ms is None else int(timeout_ms)
        self.timeout_ms = max(1, int(configured_timeout))
        self.token_timeout_s = float(token_timeout_s)
        self._connection_factory = connection_factory or http.client.HTTPSConnection
        self._connection: Any | None = None
        self._connection_lock = threading.Lock()
        if token_provider is None:
            self._bearer_provider: FoundryBearerProvider | None = FoundryBearerProvider(
                token_timeout_s=self.token_timeout_s
            )
            self._token_provider = self._bearer_provider.get_token
        else:
            self._bearer_provider = None
            self._token_provider = token_provider

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
            raise MD1Error("http", "MD1 endpoint is not configured")
        parsed = urlsplit(self.endpoint.strip())
        if parsed.scheme.lower() != "https":
            raise MD1Error("tls", "MD1 endpoint must use HTTPS")
        try:
            normalized = config.normalize_md1_endpoint(self.endpoint)
        except ValueError as exc:
            raise MD1Error("http", str(exc)) from exc
        checked = urlsplit(normalized)
        try:
            port = checked.port
        except ValueError as exc:
            raise MD1Error("http", "md1 endpoint has an invalid port") from exc
        if not checked.hostname:
            raise MD1Error("http", "md1 endpoint must include a hostname")
        return checked.hostname, port

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
    def _http_error(status: int) -> MD1Error:
        if status in (401, 403):
            return MD1Error("auth", "MD1 authentication failed")
        if status == 408:
            return MD1Error("timeout", "MD1 request timed out")
        if status in (409, 429):
            return MD1Error("rate_limited", "MD1 rate limit was exceeded")
        return MD1Error("http", f"MD1 returned HTTP {status}")

    @staticmethod
    def _transport_error(exc: BaseException) -> MD1Error:
        if isinstance(exc, (socket.timeout, TimeoutError)):
            return MD1Error("timeout", "MD1 request timed out")
        if isinstance(exc, ssl.SSLError):
            return MD1Error("tls", "MD1 TLS connection failed")
        if isinstance(exc, socket.gaierror):
            return MD1Error("dns", "MD1 hostname could not be resolved")
        return MD1Error("connection", "MD1 connection failed")

    @staticmethod
    def _idle_connection_is_stale(connection: Any) -> bool:
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
        if not self.endpoint:
            return False
        try:
            self._token_provider()
            timeout = max(0.1, self.timeout_ms / 1000.0)
            if not self._connection_lock.acquire(blocking=False):
                return True
            try:
                connection = self._get_connection_locked(timeout)
                if self._idle_connection_is_stale(connection):
                    self._close_connection_locked()
                    connection = self._get_connection_locked(timeout)
                if getattr(connection, "sock", None) is None:
                    connection.connect()
            finally:
                self._connection_lock.release()
            return True
        except (MD1Error, FoundryAuthError):
            with self._connection_lock:
                self._close_connection_locked()
            return False

    def ask(self, state: Any, questions: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
        try:
            token = self._token_provider()
        except FoundryAuthError as exc:
            raise MD1Error(exc.error_kind, str(exc)) from exc
        if not isinstance(token, str) or not token.strip():
            raise MD1Error("auth", "MD1 authentication failed")

        deadline = time.monotonic() + max(0.001, self.timeout_ms / 1000.0)
        body = json.dumps(
            {"model": self.deployment, "state": state, "questions": questions},
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {token.strip()}",
            "Content-Type": "application/json",
        }

        attempt = 0
        stale_retry_used = False
        while True:
            backoff: float | None = None
            with self._connection_lock:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MD1Error("timeout", "MD1 inference deadline exceeded")
                reused_connection = self._connection is not None
                try:
                    connection = self._get_connection_locked(remaining)
                    connection.request("POST", MD1_PATH, body=body, headers=headers)
                    response = connection.getresponse()
                except STALE_REUSE_ERRORS as exc:
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
                        decoded = json.loads(response_body)
                    except (TypeError, ValueError) as exc:
                        raise MD1Error("malformed", "MD1 response was not valid JSON") from exc
                    validated, error = validate_md1_response(decoded, questions)
                    if validated is None or error is not None:
                        raise MD1Error("malformed", error or "MD1 response validation failed")
                    return validated

                retryable = status in (408, 409, 429) or status >= 500
                if not retryable:
                    raise self._http_error(status)

                attempt += 1
                if attempt >= MAX_HTTP_ATTEMPTS:
                    raise self._http_error(status)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MD1Error("timeout", "MD1 inference deadline exceeded")
                retry_after = self._retry_after(response.getheader("Retry-After"))
                backoff = min(
                    remaining,
                    retry_after
                    if retry_after is not None
                    else min(1.0, 0.1 * (2 ** (attempt - 1)) + random.uniform(0.0, 0.1)),
                )
            time.sleep(backoff)


class MD1Engine:
    """Shape A adapter over the MD1 engine."""

    def __init__(self, client: MD1Client | None = None) -> None:
        self.client = client or MD1Client()

    @staticmethod
    def _failure_decision(
        *,
        question_id: str,
        kind: str,
        criterion: str,
        n_candidates: int,
        status: str,
        error_kind: str,
        note: str,
        tier_path: list[str],
        engine_name: str | None,
        elapsed: dict[str, float],
    ) -> dict[str, Any]:
        record = DecisionRecord(
            shape="decide",
            criterion=criterion,
            n_candidates=n_candidates,
            tier_path=tier_path,
            engine=engine_name,
            choice=None,
            choice_index=None,
            status=status,
            exit_code=2,
            raw_top_score=None,
            raw_runner_up=None,
            margin_raw=None,
            calibrated_probability=None,
            margin_calibrated=None,
            calibration_version="none",
            coverage_policy=config.coverage_policy(),
            results=[],
            elapsed_ms=elapsed,
            error_kind=error_kind,
            notes=[note],
        )
        payload = record.to_dict()
        payload.update(
            {
                "question_id": question_id,
                "kind": kind,
                "confidence": None,
                "probabilities": None,
                "margin": None,
                "noul": None,
                "score": None,
                "raw_md1_score": None,
            }
        )
        return payload

    @staticmethod
    def _failure_screen(
        *,
        criterion: str,
        n_candidates: int,
        status: str,
        error_kind: str,
        note: str,
        tier_path: list[str],
        engine_name: str | None,
        elapsed: dict[str, float],
    ) -> dict[str, Any]:
        record = DecisionRecord(
            shape="screen",
            criterion=criterion,
            n_candidates=n_candidates,
            tier_path=tier_path,
            engine=engine_name,
            choice=None,
            choice_index=None,
            status=status,
            exit_code=2,
            raw_top_score=None,
            raw_runner_up=None,
            margin_raw=None,
            calibrated_probability=None,
            margin_calibrated=None,
            calibration_version="none",
            coverage_policy=config.coverage_policy(),
            results=[],
            elapsed_ms=elapsed,
            error_kind=error_kind,
            notes=[note],
        )
        return record.to_dict()

    def decide(self, state: Any, question: Mapping[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        if not isinstance(question, Mapping):
            raise SystemOneError("invalid_input", "question must be an object")
        state_text = _serialize_state(state)
        state_payload: Any = state if isinstance(state, (dict, list)) else state_text
        instructions = _normalize_instructions(question)
        question_id_raw = question.get("id")
        question_id = str(question_id_raw).strip() if question_id_raw is not None else "q1"
        if not question_id:
            raise SystemOneError("invalid_input", "question.id must be non-empty")
        kind = str(question.get("kind") or question.get("type") or "").strip().lower()
        if kind not in {"choice", "noul", "score"}:
            raise SystemOneError("invalid_input", "question.kind must be choice, noul, or score")

        wire_question: dict[str, Any]
        candidate_ids: list[str]
        levels: list[str] | None = None
        if kind == "choice":
            options = _normalize_choice_options(question)
            candidate_ids = [option["id"] for option in options]
            wire_question = {
                "type": "choice",
                "criteria": {option["id"]: option["description"] for option in options},
            }
        elif kind == "noul":
            options = _normalize_noul_candidates(question, instructions)
            candidate_ids = ["true", "false"]
            wire_question = {
                "type": "noul",
                "criteria": {
                    "true": options[0]["description"],
                    "false": options[1]["description"],
                },
            }
        else:
            levels = _normalize_score_levels(question)
            candidate_ids = list(levels)
            wire_question = {"type": "score", "criteria": list(levels)}
        if instructions:
            wire_question["instructions"] = instructions

        engine_start = time.perf_counter()
        if self.client is None or not self.client.endpoint:
            engine_end = time.perf_counter()
            return self._failure_decision(
                question_id=question_id,
                kind=kind,
                criterion=instructions,
                n_candidates=len(candidate_ids),
                status="engine_error",
                error_kind="daemon_unavailable",
                note="ELJEV_MD1_ENDPOINT is not configured",
                tier_path=["systemone", "md1"],
                engine_name=self.client.deployment if self.client is not None else config.md1_deployment(),
                elapsed=_elapsed_ms(started, engine_start, engine_end),
            )

        try:
            response = self.client.ask(state_payload, {question_id: wire_question})
        except MD1Error as exc:
            engine_end = time.perf_counter()
            malformed = exc.error_kind == "malformed"
            return self._failure_decision(
                question_id=question_id,
                kind=kind,
                criterion=instructions,
                n_candidates=len(candidate_ids),
                status="invalid_response" if malformed else "engine_error",
                error_kind=exc.error_kind,
                note=(
                    "MD1 returned a malformed response"
                    if malformed
                    else "MD1 engine call failed"
                ),
                tier_path=["systemone", "md1"],
                engine_name=self.client.deployment,
                elapsed=_elapsed_ms(started, engine_start, engine_end),
            )
        engine_end = time.perf_counter()

        answer = response["answers"][question_id]
        raw_md1_score: float | None = None
        if kind == "choice":
            raw_probabilities = [float(answer["probabilities"][candidate_id]) for candidate_id in candidate_ids]
        elif kind == "noul":
            p_true = float(answer["noul"])
            raw_probabilities = [p_true, 1.0 - p_true]
        else:
            assert levels is not None
            raw_probabilities = [float(answer["probabilities"][str(index)]) for index in range(len(levels))]
            raw_md1_score = float(answer["score"])

        policy = config.coverage_policy()
        if policy == "calibrated":
            params, calibration_note = calibration_for(load_calibration(), kind, engine="md1")
        else:
            params, calibration_note = None, None
        temperature = float(params["temperature"]) if params is not None else 1.0
        probabilities = (
            _power_scale_probabilities(raw_probabilities, temperature)
            if params is not None
            else list(raw_probabilities)
        )
        ranked_indices = sorted(
            range(len(candidate_ids)),
            key=lambda index: (-probabilities[index], index),
        )
        top_index = ranked_indices[0]
        runner_index = ranked_indices[1] if len(ranked_indices) > 1 else None
        top_probability = float(probabilities[top_index])
        runner_probability = float(probabilities[runner_index]) if runner_index is not None else None
        gate = apply_gate(
            top=top_probability,
            runner_up=runner_probability,
            policy=policy,
            params=params,
            trusted=True,
            calibration_note=calibration_note,
        )

        choice = candidate_ids[top_index]
        choice_index: int | None = top_index
        if gate["status"] == "abstain_tie":
            choice = None
            choice_index = None

        raw_top_score = float(raw_probabilities[top_index])
        raw_runner_score = float(raw_probabilities[runner_index]) if runner_index is not None else None
        margin_raw = raw_top_score - raw_runner_score if raw_runner_score is not None else None
        margin_probability = (
            top_probability - runner_probability if runner_probability is not None else None
        )
        results = [
            {
                "id": candidate_ids[index],
                "index": index,
                "relevance_score": float(raw_probabilities[index]),
            }
            for index in ranked_indices
        ]
        record = DecisionRecord(
            shape="decide",
            criterion=instructions,
            n_candidates=len(candidate_ids),
            tier_path=["systemone", "md1"],
            engine=self.client.deployment,
            choice=choice,
            choice_index=choice_index,
            status=str(gate["status"]),
            exit_code=int(gate["exit_code"]),
            raw_top_score=raw_top_score,
            raw_runner_up=raw_runner_score,
            margin_raw=margin_raw,
            calibrated_probability=top_probability,
            margin_calibrated=margin_probability,
            calibration_version=str(gate["calibration_version"]),
            coverage_policy=policy,
            results=results,
            elapsed_ms=_elapsed_ms(started, engine_start, engine_end),
            notes=list(gate["notes"]),
        )
        payload = record.to_dict()
        payload.update(
            {
                "question_id": question_id,
                "kind": kind,
                "confidence": top_probability,
                "probabilities": {
                    candidate_ids[index]: float(probabilities[index]) for index in range(len(candidate_ids))
                },
                "margin": margin_probability,
                "noul": float(probabilities[0]) if kind == "noul" else None,
                "score": (
                    (raw_md1_score + 1.0)
                    if kind == "score" and raw_md1_score is not None and abs(temperature - 1.0) <= 1e-9
                    else (
                        sum((index + 1) * float(probabilities[index]) for index in range(len(candidate_ids)))
                        if kind == "score"
                        else None
                    )
                ),
                "raw_md1_score": raw_md1_score if kind == "score" else None,
            }
        )
        return payload

    def decide_batch(
        self,
        state: Any,
        questions: Mapping[str, Mapping[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        started = time.perf_counter()
        if not isinstance(questions, Mapping) or not questions:
            raise SystemOneError("invalid_input", "questions must be a non-empty mapping")

        state_text = _serialize_state(state)
        state_payload: Any = state if isinstance(state, (dict, list)) else state_text
        normalized_questions: dict[str, dict[str, Any]] = {}
        metadata: dict[str, dict[str, Any]] = {}
        seen_ids: set[str] = set()

        for key, question in questions.items():
            question_id = str(key).strip()
            if not question_id:
                raise SystemOneError("invalid_input", "question ids must be non-empty")
            if question_id in seen_ids:
                raise SystemOneError("invalid_input", f"duplicate question id: {question_id}")
            if not isinstance(question, Mapping):
                raise SystemOneError("invalid_input", f"question {question_id!r} must be an object")
            seen_ids.add(question_id)

            instructions = _normalize_instructions(question)
            kind = str(question.get("kind") or question.get("type") or "").strip().lower()
            if kind not in {"choice", "noul", "score"}:
                raise SystemOneError(
                    "invalid_input",
                    f"question {question_id!r}.kind must be choice, noul, or score",
                )

            wire_question: dict[str, Any]
            candidate_ids: list[str]
            if kind == "choice":
                options = _normalize_choice_options(question)
                candidate_ids = [option["id"] for option in options]
                wire_question = {
                    "type": "choice",
                    "criteria": {option["id"]: option["description"] for option in options},
                }
            elif kind == "noul":
                options = _normalize_noul_candidates(question, instructions)
                candidate_ids = ["true", "false"]
                wire_question = {
                    "type": "noul",
                    "criteria": {
                        "true": options[0]["description"],
                        "false": options[1]["description"],
                    },
                }
            else:
                levels = _normalize_score_levels(question)
                candidate_ids = list(levels)
                wire_question = {"type": "score", "criteria": list(levels)}

            if instructions:
                wire_question["instructions"] = instructions

            normalized_questions[question_id] = wire_question
            metadata[question_id] = {
                "kind": kind,
                "criterion": instructions,
                "candidate_ids": candidate_ids,
            }

        engine_start = time.perf_counter()
        if self.client is None or not self.client.endpoint:
            engine_end = time.perf_counter()
            elapsed = _elapsed_ms(started, engine_start, engine_end)
            engine_name = self.client.deployment if self.client is not None else config.md1_deployment()
            return {
                question_id: self._failure_decision(
                    question_id=question_id,
                    kind=str(meta["kind"]),
                    criterion=str(meta["criterion"]),
                    n_candidates=len(list(meta["candidate_ids"])),
                    status="engine_error",
                    error_kind="daemon_unavailable",
                    note="ELJEV_MD1_ENDPOINT is not configured",
                    tier_path=["systemone", "md1"],
                    engine_name=engine_name,
                    elapsed=elapsed,
                )
                for question_id, meta in metadata.items()
            }

        try:
            response = self.client.ask(state_payload, normalized_questions)
        except MD1Error as exc:
            engine_end = time.perf_counter()
            elapsed = _elapsed_ms(started, engine_start, engine_end)
            malformed = exc.error_kind == "malformed"
            status = "invalid_response" if malformed else "engine_error"
            note = "MD1 returned a malformed response" if malformed else "MD1 engine call failed"
            return {
                question_id: self._failure_decision(
                    question_id=question_id,
                    kind=str(meta["kind"]),
                    criterion=str(meta["criterion"]),
                    n_candidates=len(list(meta["candidate_ids"])),
                    status=status,
                    error_kind=exc.error_kind,
                    note=note,
                    tier_path=["systemone", "md1"],
                    engine_name=self.client.deployment,
                    elapsed=elapsed,
                )
                for question_id, meta in metadata.items()
            }
        engine_end = time.perf_counter()
        elapsed = _elapsed_ms(started, engine_start, engine_end)
        policy = config.coverage_policy()
        records: dict[str, dict[str, Any]] = {}
        for question_id, meta in metadata.items():
            kind = str(meta["kind"])
            criterion = str(meta["criterion"])
            candidate_ids = [str(value) for value in list(meta["candidate_ids"])]
            answer = response["answers"][question_id]
            raw_md1_score: float | None = None
            if kind == "choice":
                raw_probabilities = [float(answer["probabilities"][candidate_id]) for candidate_id in candidate_ids]
            elif kind == "noul":
                p_true = float(answer["noul"])
                raw_probabilities = [p_true, 1.0 - p_true]
            else:
                raw_probabilities = [float(answer["probabilities"][str(index)]) for index in range(len(candidate_ids))]
                raw_md1_score = float(answer["score"])

            records[question_id] = self._gate_record(
                question_id=question_id,
                kind=kind,
                criterion=criterion,
                candidate_ids=candidate_ids,
                raw_probabilities=raw_probabilities,
                policy=policy,
                elapsed=elapsed,
                raw_md1_score=raw_md1_score,
            )
        return records

    def screen(self, criterion: str, candidates: Sequence[Candidate], *, top_n: int | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        candidate_list = list(candidates)
        pregate_started = time.perf_counter()
        pregate_result = check_pregate(candidate_list)
        pregate_ended = time.perf_counter()
        policy = config.coverage_policy()
        if pregate_result.trivial:
            record = DecisionRecord(
                shape="screen",
                criterion=criterion,
                n_candidates=len(candidate_list),
                tier_path=["pregate"],
                status="trivial",
                exit_code=2,
                notes=[pregate_result.reason or "local pre-gate"],
                elapsed_ms=_elapsed_ms(started, pregate_started, pregate_ended),
                coverage_policy=policy,
            )
            return record.to_dict()

        if self.client is None or not self.client.endpoint:
            return self._failure_screen(
                criterion=criterion,
                n_candidates=len(candidate_list),
                status="engine_error",
                error_kind="daemon_unavailable",
                note="ELJEV_MD1_ENDPOINT is not configured",
                tier_path=["pregate", "md1"],
                engine_name=self.client.deployment if self.client is not None else config.md1_deployment(),
                elapsed=_elapsed_ms(started, pregate_started, pregate_ended),
            )

        candidate_map = {candidate.id: candidate.text for candidate in candidate_list}
        engine_started = time.perf_counter()
        try:
            response = self.client.ask(
                "Select the candidate that best satisfies the criterion.",
                {
                    "screen": {
                        "type": "choice",
                        "instructions": criterion,
                        "criteria": candidate_map,
                    }
                },
            )
        except MD1Error as exc:
            engine_ended = time.perf_counter()
            malformed = exc.error_kind == "malformed"
            return self._failure_screen(
                criterion=criterion,
                n_candidates=len(candidate_list),
                status="invalid_response" if malformed else "engine_error",
                error_kind=exc.error_kind,
                note=(
                    "MD1 returned a malformed response"
                    if malformed
                    else "MD1 engine call failed"
                ),
                tier_path=["pregate", "md1"],
                engine_name=self.client.deployment,
                elapsed=_elapsed_ms(started, engine_started, engine_ended),
            )
        engine_ended = time.perf_counter()

        answer = response["answers"]["screen"]
        candidate_ids = [candidate.id for candidate in candidate_list]
        raw_probabilities = [float(answer["probabilities"][candidate_id]) for candidate_id in candidate_ids]
        if policy == "calibrated":
            params, calibration_note = calibration_for(load_calibration(), "screen", engine="md1")
        else:
            params, calibration_note = None, None
        temperature = float(params["temperature"]) if params is not None else 1.0
        probabilities = (
            _power_scale_probabilities(raw_probabilities, temperature)
            if params is not None
            else list(raw_probabilities)
        )
        ranked_indices = sorted(
            range(len(candidate_ids)),
            key=lambda index: (-probabilities[index], index),
        )
        top_index = ranked_indices[0]
        runner_index = ranked_indices[1] if len(ranked_indices) > 1 else None
        top_probability = float(probabilities[top_index])
        runner_probability = float(probabilities[runner_index]) if runner_index is not None else None
        gate = apply_gate(
            top=top_probability,
            runner_up=runner_probability,
            policy=policy,
            params=params,
            trusted=True,
            calibration_note=calibration_note,
        )

        choice = candidate_ids[top_index]
        choice_index: int | None = top_index
        if gate["status"] == "abstain_tie":
            choice = None
            choice_index = None

        raw_top_score = float(raw_probabilities[top_index])
        raw_runner_score = float(raw_probabilities[runner_index]) if runner_index is not None else None
        margin_raw = raw_top_score - raw_runner_score if raw_runner_score is not None else None
        margin_probability = (
            top_probability - runner_probability if runner_probability is not None else None
        )
        limit = len(ranked_indices) if top_n is None else max(0, min(int(top_n), len(ranked_indices)))
        results = [
            {
                "id": candidate_ids[index],
                "index": index,
                "relevance_score": float(raw_probabilities[index]),
            }
            for index in ranked_indices[:limit]
        ]
        record = DecisionRecord(
            shape="screen",
            criterion=criterion,
            n_candidates=len(candidate_list),
            tier_path=["pregate", "md1"],
            engine=self.client.deployment,
            choice=choice,
            choice_index=choice_index,
            status=str(gate["status"]),
            exit_code=int(gate["exit_code"]),
            raw_top_score=raw_top_score,
            raw_runner_up=raw_runner_score,
            margin_raw=margin_raw,
            calibrated_probability=top_probability,
            margin_calibrated=margin_probability,
            calibration_version=str(gate["calibration_version"]),
            coverage_policy=policy,
            results=results,
            elapsed_ms=_elapsed_ms(started, engine_started, engine_ended),
            notes=list(gate["notes"]),
        )
        return record.to_dict()

    def gate(self, state: Any, *, session_id: str | None = None) -> dict[str, Any]:
        started = time.perf_counter()
        policy = config.coverage_policy()
        ext_mode = config.extension_mode()
        default_engine = self.client.deployment if self.client is not None else config.md1_deployment()
        state_text = _serialize_state(state)
        state_payload: Any = state if isinstance(state, (dict, list)) else state_text

        if self.client is None or not self.client.endpoint:
            elapsed = round((time.perf_counter() - started) * 1000.0, 3)
            return {
                "schema": "eljev.gate/1",
                "session_id": session_id,
                "engine": default_engine,
                "status": "engine_error",
                "exit_code": 2,
                "tier": "deep_system2",
                "intent": None,
                "needs_deep_analysis": None,
                "enforceable": False,
                "extension_mode": ext_mode,
                "coverage_policy": policy,
                "budget_text": _budget_text("deep_system2", False, "engine_error"),
                "decisions": {},
                "elapsed_ms": elapsed,
                "error_kind": "daemon_unavailable",
                "notes": ["ELJEV_MD1_ENDPOINT is not configured"],
            }
        questions = {
            "intent": {
                "type": "choice",
                "instructions": "Classify the request intent",
                "criteria": INTENT_CRITERIA,
            },
            "needs_deep_analysis": {
                "type": "noul",
                "instructions": "Determine whether deeper deliberation is required before execution",
                "criteria": DEEP_ANALYSIS_CRITERIA,
            },
            "execution_tier": {
                "type": "choice",
                "instructions": "Select the execution tier for this request",
                "criteria": EXECUTION_TIER_CRITERIA,
            },
        }

        try:
            response = self.client.ask(state_payload, questions)
        except MD1Error as exc:
            status = "invalid_response" if exc.error_kind == "malformed" else "engine_error"
            note = (
                "MD1 returned a malformed response"
                if exc.error_kind == "malformed"
                else "MD1 engine call failed"
            )
            elapsed = round((time.perf_counter() - started) * 1000.0, 3)
            return {
                "schema": "eljev.gate/1",
                "session_id": session_id,
                "engine": self.client.deployment,
                "status": status,
                "exit_code": 2,
                "tier": "deep_system2",
                "intent": None,
                "needs_deep_analysis": None,
                "enforceable": False,
                "extension_mode": ext_mode,
                "coverage_policy": policy,
                "budget_text": _budget_text("deep_system2", False, status),
                "decisions": {},
                "elapsed_ms": elapsed,
                "error_kind": exc.error_kind,
                "notes": [note],
            }

        elapsed_record = _elapsed_ms(started, started, time.perf_counter())
        intent_record = self._gate_record(
            question_id="intent",
            kind="choice",
            criterion="Classify the request intent",
            candidate_ids=list(INTENT_CRITERIA.keys()),
            raw_probabilities=[
                float(response["answers"]["intent"]["probabilities"][candidate_id])
                for candidate_id in INTENT_CRITERIA.keys()
            ],
            policy=policy,
            elapsed=elapsed_record,
        )
        noul_answer = float(response["answers"]["needs_deep_analysis"]["noul"])
        noul_record = self._gate_record(
            question_id="needs_deep_analysis",
            kind="noul",
            criterion="Determine whether deeper deliberation is required before execution",
            candidate_ids=["true", "false"],
            raw_probabilities=[noul_answer, 1.0 - noul_answer],
            policy=policy,
            elapsed=elapsed_record,
        )
        tier_record = self._gate_record(
            question_id="execution_tier",
            kind="choice",
            criterion="Select the execution tier for this request",
            candidate_ids=list(EXECUTION_TIER_CRITERIA.keys()),
            raw_probabilities=[
                float(response["answers"]["execution_tier"]["probabilities"][candidate_id])
                for candidate_id in EXECUTION_TIER_CRITERIA.keys()
            ],
            policy=policy,
            elapsed=elapsed_record,
        )

        decisions = {
            "intent": intent_record,
            "needs_deep_analysis": noul_record,
            "execution_tier": tier_record,
        }
        enforceable_heads_selected = all(
            record.get("status") == "selected" for record in decisions.values()
        )
        overall_status = "selected" if enforceable_heads_selected else "needs_review"
        tier_choice = tier_record.get("choice")
        resolved_tier = (
            str(tier_choice)
            if isinstance(tier_choice, str) and tier_choice in EXECUTION_TIER_CRITERIA
            else "deep_system2"
        )
        if noul_record.get("choice") == "true":
            resolved_tier = "deep_system2"
        enforceable = bool(
            ext_mode == "enforce"
            and enforceable_heads_selected
            and resolved_tier == "fast_direct"
            and noul_record.get("choice") == "false"
        )
        notes: list[str] = []
        for record in decisions.values():
            for note in record.get("notes", []):
                if note not in notes:
                    notes.append(str(note))
        elapsed = round((time.perf_counter() - started) * 1000.0, 3)
        return {
            "schema": "eljev.gate/1",
            "session_id": session_id,
            "engine": self.client.deployment if self.client else config.md1_deployment(),
            "status": overall_status,
            "exit_code": 0 if enforceable_heads_selected else 2,
            "tier": resolved_tier,
            "intent": intent_record.get("choice"),
            "needs_deep_analysis": noul_record.get("noul"),
            "enforceable": enforceable,
            "extension_mode": ext_mode,
            "coverage_policy": policy,
            "budget_text": _budget_text(
                resolved_tier,
                enforceable,
                overall_status,
                intent=intent_record.get("choice"),
                needs_deep_analysis=noul_record.get("noul"),
            ),
            "decisions": decisions,
            "elapsed_ms": elapsed,
            "error_kind": None,
            "notes": notes,
        }

    def _gate_record(
        self,
        *,
        question_id: str,
        kind: str,
        criterion: str,
        candidate_ids: list[str],
        raw_probabilities: list[float],
        policy: str,
        elapsed: Mapping[str, float],
        raw_md1_score: float | None = None,
    ) -> dict[str, Any]:
        if policy == "calibrated":
            params, calibration_note = calibration_for(load_calibration(), kind, engine="md1")
        else:
            params, calibration_note = None, None
        temperature = float(params["temperature"]) if params is not None else 1.0
        probabilities = (
            _power_scale_probabilities(raw_probabilities, temperature)
            if params is not None
            else list(raw_probabilities)
        )
        ranked_indices = sorted(
            range(len(candidate_ids)),
            key=lambda index: (-probabilities[index], index),
        )
        top_index = ranked_indices[0]
        runner_index = ranked_indices[1] if len(ranked_indices) > 1 else None
        top_probability = float(probabilities[top_index])
        runner_probability = float(probabilities[runner_index]) if runner_index is not None else None
        gate = apply_gate(
            top=top_probability,
            runner_up=runner_probability,
            policy=policy,
            params=params,
            trusted=True,
            calibration_note=calibration_note,
        )

        choice = candidate_ids[top_index]
        choice_index: int | None = top_index
        if gate["status"] == "abstain_tie":
            choice = None
            choice_index = None
        raw_top_score = float(raw_probabilities[top_index])
        raw_runner_score = float(raw_probabilities[runner_index]) if runner_index is not None else None
        margin_raw = raw_top_score - raw_runner_score if raw_runner_score is not None else None
        margin_probability = (
            top_probability - runner_probability if runner_probability is not None else None
        )
        results = [
            {
                "id": candidate_ids[index],
                "index": index,
                "relevance_score": float(raw_probabilities[index]),
            }
            for index in ranked_indices
        ]
        record = DecisionRecord(
            shape="decide",
            criterion=criterion,
            n_candidates=len(candidate_ids),
            tier_path=["systemone", "md1"],
            engine=self.client.deployment if self.client is not None else config.md1_deployment(),
            choice=choice,
            choice_index=choice_index,
            status=str(gate["status"]),
            exit_code=int(gate["exit_code"]),
            raw_top_score=raw_top_score,
            raw_runner_up=raw_runner_score,
            margin_raw=margin_raw,
            calibrated_probability=top_probability,
            margin_calibrated=margin_probability,
            calibration_version=str(gate["calibration_version"]),
            coverage_policy=policy,
            results=results,
            elapsed_ms={
                "total": float(elapsed.get("total", 0.0)),
                "pregate": float(elapsed.get("pregate", 0.0)),
                "engine": float(elapsed.get("engine", 0.0)),
                "overhead": float(elapsed.get("overhead", 0.0)),
            },
            notes=list(gate["notes"]),
        )
        payload = record.to_dict()
        payload.update(
            {
                "question_id": question_id,
                "kind": kind,
                "confidence": top_probability,
                "probabilities": {
                    candidate_ids[index]: float(probabilities[index]) for index in range(len(candidate_ids))
                },
                "margin": margin_probability,
                "noul": float(probabilities[0]) if kind == "noul" else None,
                "score": (
                    (raw_md1_score + 1.0)
                    if kind == "score" and raw_md1_score is not None and abs(temperature - 1.0) <= 1e-9
                    else (
                        sum((index + 1) * float(probabilities[index]) for index in range(len(candidate_ids)))
                        if kind == "score"
                        else None
                    )
                ),
                "raw_md1_score": raw_md1_score if kind == "score" else None,
            }
        )
        return payload


def _budget_text(
    tier: str,
    enforceable: bool,
    status: str,
    intent: str | None = None,
    needs_deep_analysis: float | None = None,
) -> str:
    if status not in {"selected", "needs_review"}:
        directive = "Gate unavailable; proceed with normal caution."
    elif tier == "fast_direct":
        directive = "Use direct local actions only (1-3 direct tool calls, no sub-agents)."
    elif tier == "bounded_work":
        directive = "Keep execution bounded to a single track in <=5 direct tool calls."
    else:
        directive = "Deep analysis and multi-step execution are acceptable."
    mode = "enforce" if enforceable else "advisory"
    intent_text = intent if isinstance(intent, str) and intent.strip() else "unknown"
    deep_text = (
        f"{float(needs_deep_analysis):.3f}"
        if isinstance(needs_deep_analysis, (int, float)) and not isinstance(needs_deep_analysis, bool)
        else "unknown"
    )
    return (
        "[el-mds gate]\n"
        f"tier: {tier} | intent: {intent_text} | needs_deep_analysis: {deep_text} | mode: {mode} ({status})\n"
        f"directive: {directive}\n"
        "[/el-mds gate]"
    )
