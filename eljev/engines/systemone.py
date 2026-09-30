"""Shape A System One decision engine for typed choices, noul, and scores."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import math
import os
import re
import socket
import ssl
import time
from typing import Any
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from .. import config
from ..types import DecisionRecord
from ..verdict import apply_gate, calibration_for, load_calibration, validate_engine_response
from .cohere import CohereError


DISPLAY_TEMPERATURE = 0.05
MAX_STATE_CHARS = 100_000
MAX_INSTRUCTIONS_CHARS = 2_000
MIN_OPTIONS = 2
MAX_OPTIONS = 250
MAX_OPTION_CHARS = 2_000
MIN_SCORE_LEVELS = 2
MAX_SCORE_LEVELS = 10


class SystemOneError(RuntimeError):
    def __init__(self, error_kind: str, message: str) -> None:
        super().__init__(message)
        self.error_kind = error_kind


class SystemOneClient:
    """POST typed decisions to ``<base>/v1/systemone``."""

    def __init__(self, endpoint: str | None = None, *, timeout_s: float = 2.5) -> None:
        self.endpoint = endpoint or os.environ.get("ELJEV_SYSTEMONE_URL", "")
        self.timeout_s = timeout_s

    def decide(self, state: str, question: Mapping[str, Any]) -> dict[str, Any]:
        if not self.endpoint:
            raise SystemOneError("daemon_unavailable", "System One endpoint is not configured")
        parsed = urlsplit(self.endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise SystemOneError("connection", "System One endpoint is invalid")
        url = self.endpoint.rstrip("/") + "/v1/systemone"
        payload = json.dumps({"state": state, "question": question}).encode("utf-8")
        request = Request(
            url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                body = response.read()
        except TimeoutError as exc:
            raise SystemOneError("timeout", "System One request timed out") from exc
        except ssl.SSLError as exc:
            raise SystemOneError("tls", "System One TLS connection failed") from exc
        except socket.gaierror as exc:
            raise SystemOneError("dns", "System One hostname could not be resolved") from exc
        except OSError as exc:
            raise SystemOneError("connection", "System One connection failed") from exc
        try:
            decoded = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise SystemOneError("malformed", "System One returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise SystemOneError("malformed", "System One returned a non-object response")
        return decoded


def _local_token_match(query: str, documents: list[str]) -> list[float]:
    query_tokens = set(re.findall(r"\w+", query.lower()))
    scores: list[float] = []
    for doc in documents:
        doc_tokens = set(re.findall(r"\w+", doc.lower()))
        if not doc_tokens:
            scores.append(0.0)
            continue
        intersection = query_tokens.intersection(doc_tokens)
        score = len(intersection) / math.sqrt(len(query_tokens) * len(doc_tokens) + 1.0)
        scores.append(max(0.0, float(score)))
    return scores


def _softmax(scores: list[float], temperature: float = DISPLAY_TEMPERATURE) -> list[float]:
    if not scores:
        return []
    safe_temperature = max(1e-4, float(temperature))
    scaled = [score / safe_temperature for score in scores]
    max_scaled = max(scaled)
    exps = [math.exp(value - max_scaled) for value in scaled]
    exp_sum = sum(exps)
    if exp_sum <= 0:
        return [1.0 / len(scores)] * len(scores)
    return [value / exp_sum for value in exps]


def _proportional(scores: list[float]) -> list[float]:
    if not scores:
        return []
    total = sum(score for score in scores if score > 0.0)
    if total <= 0.0:
        return [1.0 / len(scores)] * len(scores)
    return [max(0.0, score) / total for score in scores]


def _elapsed_ms(start: float, engine_start: float, engine_end: float) -> dict[str, float]:
    total_ms = (time.perf_counter() - start) * 1000.0
    engine_ms = max(0.0, (engine_end - engine_start) * 1000.0)
    return {
        "total": round(total_ms, 3),
        "pregate": 0.0,
        "engine": round(engine_ms, 3),
        "overhead": round(max(0.0, total_ms - engine_ms), 3),
    }


def _serialize_state(state: Any) -> str:
    if isinstance(state, str):
        state_text = state
    elif isinstance(state, (dict, list)):
        try:
            state_text = json.dumps(state, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise SystemOneError("invalid_input", "state must be JSON-serializable") from exc
    else:
        raise SystemOneError("invalid_input", "state must be a non-empty string or JSON object/array")
    if not state_text.strip():
        raise SystemOneError("invalid_input", "state must not be empty")
    if len(state_text) > MAX_STATE_CHARS:
        raise SystemOneError("invalid_input", f"state exceeds {MAX_STATE_CHARS} characters")
    return state_text


def _normalize_instructions(question: Mapping[str, Any]) -> str:
    raw = question.get("instructions")
    if raw is None:
        raw = question.get("description")
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise SystemOneError("invalid_input", "question instructions must be a string")
    text = raw.strip()
    if len(text) > MAX_INSTRUCTIONS_CHARS:
        raise SystemOneError(
            "invalid_input", f"instructions exceed {MAX_INSTRUCTIONS_CHARS} characters"
        )
    return text


def _normalize_choice_options(question: Mapping[str, Any]) -> list[dict[str, str]]:
    options_raw = question.get("options")
    criteria_raw = question.get("criteria")
    options: list[dict[str, str]] = []
    if isinstance(options_raw, list):
        for index, option in enumerate(options_raw):
            if isinstance(option, str):
                option_id = option.strip()
                description = option.strip()
            elif isinstance(option, Mapping):
                option_id = str(option.get("id") or option.get("name") or "").strip()
                option_text = option.get("description")
                if option_text is None:
                    option_text = option.get("text")
                description = str(option_text or "").strip()
            else:
                raise SystemOneError("invalid_input", f"choice option {index} must be a string or object")
            if not option_id:
                raise SystemOneError("invalid_input", f"choice option {index} id must be non-empty")
            if not description:
                raise SystemOneError(
                    "invalid_input", f"choice option {index} description must be non-empty"
                )
            if len(description) > MAX_OPTION_CHARS:
                raise SystemOneError(
                    "invalid_input",
                    f"choice option {index} exceeds {MAX_OPTION_CHARS} characters",
                )
            options.append({"id": option_id, "description": description})
    elif isinstance(criteria_raw, Mapping):
        for option_id, description in criteria_raw.items():
            option_text = str(description).strip()
            option_name = str(option_id).strip()
            if not option_name:
                raise SystemOneError("invalid_input", "choice option id must be non-empty")
            if not option_text:
                raise SystemOneError(
                    "invalid_input", f"choice option {option_name!r} description must be non-empty"
                )
            if len(option_text) > MAX_OPTION_CHARS:
                raise SystemOneError(
                    "invalid_input",
                    f"choice option {option_name!r} exceeds {MAX_OPTION_CHARS} characters",
                )
            options.append({"id": option_name, "description": option_text})
    else:
        raise SystemOneError(
            "invalid_input", "choice question must provide options list or criteria mapping"
        )
    if len(options) < MIN_OPTIONS or len(options) > MAX_OPTIONS:
        raise SystemOneError(
            "invalid_input", f"choice question must provide {MIN_OPTIONS}..{MAX_OPTIONS} options"
        )
    seen_ids: set[str] = set()
    for option in options:
        option_id = option["id"]
        if option_id in seen_ids:
            raise SystemOneError("invalid_input", f"duplicate choice option id: {option_id}")
        seen_ids.add(option_id)
    return options


def _normalize_noul_candidates(question: Mapping[str, Any], instructions: str) -> list[dict[str, str]]:
    criteria = question.get("criteria")
    if isinstance(criteria, Mapping):
        true_text = str(criteria.get("true") or "").strip()
        false_text = str(criteria.get("false") or "").strip()
    else:
        true_text = ""
        false_text = ""
    if not true_text:
        true_text = f"True: {instructions}" if instructions else "True"
    if not false_text:
        false_text = f"False: not {instructions}" if instructions else "False"
    for index, text in enumerate((true_text, false_text)):
        if not text:
            raise SystemOneError("invalid_input", f"noul criterion {index} must be non-empty")
        if len(text) > MAX_OPTION_CHARS:
            raise SystemOneError(
                "invalid_input", f"noul criterion {index} exceeds {MAX_OPTION_CHARS} characters"
            )
    return [{"id": "true", "description": true_text}, {"id": "false", "description": false_text}]


def _normalize_score_levels(question: Mapping[str, Any]) -> list[str]:
    criteria = question.get("criteria")
    if not isinstance(criteria, Sequence) or isinstance(criteria, (str, bytes, bytearray)):
        raise SystemOneError("invalid_input", "score question must provide a criteria list")
    levels = [str(item).strip() for item in criteria]
    if len(levels) < MIN_SCORE_LEVELS or len(levels) > MAX_SCORE_LEVELS:
        raise SystemOneError(
            "invalid_input",
            f"score question must provide {MIN_SCORE_LEVELS}..{MAX_SCORE_LEVELS} levels",
        )
    for index, level in enumerate(levels):
        if not level:
            raise SystemOneError("invalid_input", f"score level {index} must be non-empty")
        if len(level) > MAX_OPTION_CHARS:
            raise SystemOneError(
                "invalid_input", f"score level {index} exceeds {MAX_OPTION_CHARS} characters"
            )
    return levels


class SystemOneEngine:
    """Built-in decision engine for typed shape A decisions."""

    def __init__(
        self,
        cohere: Any | None = None,
        passthrough_url: str | None = None,
        display_temperature: float = DISPLAY_TEMPERATURE,
    ) -> None:
        self.cohere = cohere
        self.passthrough_url = passthrough_url or os.environ.get("ELJEV_SYSTEMONE_URL", "")
        self.passthrough_client = SystemOneClient(self.passthrough_url) if self.passthrough_url else None
        self.display_temperature = display_temperature

    def decide(self, state: Any, question: Mapping[str, Any]) -> dict[str, Any]:
        if self.passthrough_client is not None:
            state_text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
            return self.passthrough_client.decide(state_text, question)

        if not isinstance(question, Mapping):
            raise SystemOneError("invalid_input", "question must be an object")
        state_text = _serialize_state(state)
        instructions = _normalize_instructions(question)
        question_id_raw = question.get("id")
        question_id = str(question_id_raw).strip() if question_id_raw is not None else "q1"
        if not question_id:
            raise SystemOneError("invalid_input", "question.id must be non-empty")
        kind = str(question.get("kind") or question.get("type") or "").strip().lower()
        if kind not in {"choice", "noul", "score"}:
            raise SystemOneError("invalid_input", "question.kind must be choice, noul, or score")

        if kind == "choice":
            options = _normalize_choice_options(question)
            candidate_ids = [option["id"] for option in options]
            candidate_texts = [option["description"] for option in options]
            query = self._build_query(state_text, instructions)
            return self._evaluate(
                question_id=question_id,
                kind=kind,
                criterion=instructions,
                query=query,
                candidate_ids=candidate_ids,
                candidate_texts=candidate_texts,
            )

        if kind == "noul":
            options = _normalize_noul_candidates(question, instructions)
            candidate_ids = [option["id"] for option in options]
            candidate_texts = [option["description"] for option in options]
            query = self._build_query(state_text, instructions)
            return self._evaluate(
                question_id=question_id,
                kind=kind,
                criterion=instructions,
                query=query,
                candidate_ids=candidate_ids,
                candidate_texts=candidate_texts,
            )

        levels = _normalize_score_levels(question)
        query = self._build_query(state_text, instructions)
        return self._evaluate(
            question_id=question_id,
            kind=kind,
            criterion=instructions,
            query=query,
            candidate_ids=levels,
            candidate_texts=levels,
        )

    @staticmethod
    def _build_query(state: str, instructions: str) -> str:
        if instructions:
            return f"State: {state}\nQuestion: {instructions}"
        return f"State: {state}"

    def _score_candidates(
        self, query: str, candidate_texts: list[str]
    ) -> tuple[list[float], str, list[str], bool]:
        if self.cohere is not None and getattr(self.cohere, "endpoint", ""):
            rerank_response = self.cohere.rerank(query, candidate_texts, len(candidate_texts))
            validated, error = validate_engine_response(rerank_response, len(candidate_texts))
            if validated is None or error is not None:
                raise CohereError("malformed", error or "invalid response")
            if len(validated) != len(candidate_texts):
                # Softmax needs a dense score vector; zero-filling omitted candidates
                # would fabricate a wide margin, so a partial ranking fails closed.
                raise CohereError(
                    "malformed",
                    f"provider scored {len(validated)} of {len(candidate_texts)} candidates",
                )
            scores = [0.0] * len(candidate_texts)
            for item in validated:
                scores[item["index"]] = float(item["relevance_score"])
            engine_name = str(getattr(self.cohere, "deployment", config.cohere_deployment()))
            return scores, engine_name, ["systemone", "cohere"], True

        scores = _local_token_match(query, candidate_texts)
        return scores, "local_heuristic", ["systemone", "local_heuristic"], False

    def _build_failure_record(
        self,
        *,
        question_id: str,
        kind: str,
        criterion: str,
        tier_path: list[str],
        engine_name: str,
        status: str,
        error_kind: str,
        note: str,
        elapsed: dict[str, float],
    ) -> dict[str, Any]:
        record = DecisionRecord(
            shape="decide",
            criterion=criterion,
            n_candidates=0,
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
            }
        )
        return payload

    def _evaluate(
        self,
        *,
        question_id: str,
        kind: str,
        criterion: str,
        query: str,
        candidate_ids: list[str],
        candidate_texts: list[str],
    ) -> dict[str, Any]:
        started = time.perf_counter()
        engine_started = time.perf_counter()
        try:
            raw_scores, engine_name, tier_path, trusted = self._score_candidates(query, candidate_texts)
        except CohereError as exc:
            engine_ended = time.perf_counter()
            return self._build_failure_record(
                question_id=question_id,
                kind=kind,
                criterion=criterion,
                tier_path=["systemone", "cohere"],
                engine_name=config.cohere_deployment(),
                status="invalid_response" if exc.error_kind == "malformed" else "engine_error",
                error_kind=exc.error_kind,
                note=(
                    "Cohere returned a malformed response"
                    if exc.error_kind == "malformed"
                    else "Cohere engine call failed"
                ),
                elapsed=_elapsed_ms(started, engine_started, engine_ended),
            )
        engine_ended = time.perf_counter()

        calibration = load_calibration()
        params, calibration_note = calibration_for(calibration, kind)
        temperature = float(params["temperature"]) if params is not None else self.display_temperature
        probabilities = (
            _softmax(raw_scores, temperature=temperature) if trusted else _proportional(raw_scores)
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
            policy=config.coverage_policy(),
            params=params,
            trusted=trusted,
            calibration_note=calibration_note,
        )

        choice = candidate_ids[top_index]
        choice_index: int | None = top_index
        if gate["status"] == "abstain_tie":
            choice = None
            choice_index = None

        raw_top_score = float(raw_scores[top_index])
        raw_runner_score = float(raw_scores[runner_index]) if runner_index is not None else None
        margin_raw = (
            raw_top_score - raw_runner_score if raw_runner_score is not None else None
        )
        margin_probability = (
            top_probability - runner_probability if runner_probability is not None else None
        )
        results = [
            {
                "id": candidate_ids[index],
                "index": index,
                "relevance_score": float(raw_scores[index]),
            }
            for index in ranked_indices
        ]
        record = DecisionRecord(
            shape="decide",
            criterion=criterion,
            n_candidates=len(candidate_ids),
            tier_path=tier_path,
            engine=engine_name,
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
            coverage_policy=config.coverage_policy(),
            results=results,
            elapsed_ms=_elapsed_ms(started, engine_started, engine_ended),
            notes=list(gate["notes"]),
        )
        payload = record.to_dict()
        payload.update(
            {
                "question_id": question_id,
                "kind": kind,
                "confidence": top_probability,
                "probabilities": {
                    candidate_ids[index]: float(probabilities[index])
                    for index in range(len(candidate_ids))
                },
                "margin": margin_probability,
                "noul": float(probabilities[0]) if kind == "noul" else None,
                "score": (
                    sum((index + 1) * float(probabilities[index]) for index in range(len(candidate_ids)))
                    if kind == "score"
                    else None
                ),
            }
        )
        return payload
