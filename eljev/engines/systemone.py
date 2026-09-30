"""Shape A System One decision engine for typed choices, noul (boolean gates), and scores.

Supports:
1. Built-in classification using Cohere Rerank on Azure AI Foundry when configured.
2. Passthrough to external System One endpoint when ELJEV_SYSTEMONE_URL is configured.
3. Fast deterministic local heuristic fallback when offline.
"""

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
from uuid import uuid4

from ..types import SCHEMA, utc_timestamp


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
        scores.append(round(score, 6))
    return scores


def _softmax(scores: list[float], temperature: float = 0.05) -> list[float]:
    if not scores:
        return []
    T = max(1e-4, float(temperature))
    scaled = [s / T for s in scores]
    max_z = max(scaled)
    exps = [math.exp(z - max_z) for z in scaled]
    sum_exps = sum(exps)
    if sum_exps <= 0:
        return [1.0 / len(scores)] * len(scores)
    return [round(e / sum_exps, 4) for e in exps]


class SystemOneEngine:
    """Built-in decision engine mimicking TypeSafeAI Jev System One."""

    def __init__(
        self,
        cohere: Any | None = None,
        passthrough_url: str | None = None,
        default_temperature: float = 0.05,
    ) -> None:
        self.cohere = cohere
        self.passthrough_url = passthrough_url or os.environ.get("ELJEV_SYSTEMONE_URL", "")
        self.passthrough_client = SystemOneClient(self.passthrough_url) if self.passthrough_url else None
        self.default_temperature = default_temperature

    def decide(self, state: Any, question: Mapping[str, Any]) -> dict[str, Any]:
        if self.passthrough_client is not None:
            state_text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
            return self.passthrough_client.decide(state_text, question)

        start_time = time.perf_counter()
        state_str = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
        question_id = str(question.get("id") or "q1")
        kind = str(question.get("kind") or question.get("type") or "choice").lower()
        instructions = str(question.get("instructions") or question.get("description") or "").strip()

        if kind == "choice":
            return self._decide_choice(start_time, state_str, question_id, instructions, question)
        elif kind == "noul":
            return self._decide_noul(start_time, state_str, question_id, instructions, question)
        elif kind == "score":
            return self._decide_score(start_time, state_str, question_id, instructions, question)
        else:
            raise SystemOneError("invalid_input", f"Unsupported question kind: {kind}")

    def _score_candidates(
        self, query: str, candidate_texts: list[str]
    ) -> tuple[list[float], str, list[str]]:
        tier_path = ["systemone"]
        engine_name = "local_heuristic"
        scores = [0.0] * len(candidate_texts)

        if self.cohere is not None and getattr(self.cohere, "endpoint", None):
            try:
                rerank_resp = self.cohere.rerank(query, candidate_texts, len(candidate_texts))
                results = rerank_resp.get("results", [])
                for item in results:
                    idx = item.get("index")
                    if isinstance(idx, int) and 0 <= idx < len(scores):
                        scores[idx] = float(item.get("relevance_score", 0.0))
                tier_path.append("cohere")
                engine_name = getattr(self.cohere, "deployment", "cohere-rerank-v4.0-pro")
                return scores, engine_name, tier_path
            except Exception:
                tier_path.append("local_heuristic_fallback")
        else:
            tier_path.append("local_heuristic")

        scores = _local_token_match(query, candidate_texts)
        return scores, engine_name, tier_path

    def _decide_choice(
        self,
        start_time: float,
        state: str,
        question_id: str,
        instructions: str,
        question: Mapping[str, Any],
    ) -> dict[str, Any]:
        options_raw = question.get("options")
        criteria_raw = question.get("criteria")

        options: list[dict[str, str]] = []
        if isinstance(options_raw, list):
            for opt in options_raw:
                if isinstance(opt, Mapping):
                    opt_id = str(opt.get("id") or opt.get("name") or "")
                    desc = str(opt.get("description") or opt.get("text") or opt_id)
                    options.append({"id": opt_id, "description": desc})
                elif isinstance(opt, str):
                    options.append({"id": opt, "description": opt})
        elif isinstance(criteria_raw, Mapping):
            for opt_id, desc in criteria_raw.items():
                options.append({"id": str(opt_id), "description": str(desc)})

        if not options:
            raise SystemOneError("invalid_input", "choice question must provide options or criteria")

        query = f"State: {state}\nQuestion: {instructions}" if instructions else f"State: {state}"
        candidate_texts = [
            f"{opt['id']}: {opt['description']}"
            if opt["description"] and opt["description"] != opt["id"]
            else opt["id"]
            for opt in options
        ]

        scores, engine_name, tier_path = self._score_candidates(query, candidate_texts)
        temperature = float(question.get("temperature") or self.default_temperature)
        probs = _softmax(scores, temperature)

        top_idx = max(range(len(probs)), key=lambda i: probs[i])
        choice = options[top_idx]["id"]
        confidence = probs[top_idx]
        probabilities = {opt["id"]: p for opt, p in zip(options, probs)}

        sorted_probs = sorted(probs, reverse=True)
        margin = sorted_probs[0] - sorted_probs[1] if len(sorted_probs) > 1 else sorted_probs[0]

        policy = os.environ.get("ELJEV_COVERAGE_POLICY", "always_abstain_v0")
        if policy == "calibrated":
            if confidence >= 0.60 and (len(options) == 1 or margin >= 0.10):
                status = "selected"
                exit_code = 0
            else:
                status = "needs_review"
                exit_code = 2
        else:
            status = "needs_review"
            exit_code = 2

        elapsed = round((time.perf_counter() - start_time) * 1000.0, 3)
        ranked_results = [
            {"id": opt["id"], "index": idx, "relevance_score": scores[idx], "probability": probs[idx]}
            for idx, opt in enumerate(options)
        ]
        ranked_results.sort(key=lambda item: item["probability"], reverse=True)

        return {
            "schema": SCHEMA,
            "decision_id": str(uuid4()),
            "ts": utc_timestamp(),
            "shape": "decide",
            "question_id": question_id,
            "kind": "choice",
            "criterion": instructions,
            "n_candidates": len(options),
            "tier_path": tier_path,
            "engine": engine_name,
            "choice": choice,
            "choice_index": top_idx,
            "status": status,
            "exit_code": exit_code,
            "confidence": confidence,
            "probabilities": probabilities,
            "margin": round(margin, 4),
            "results": ranked_results,
            "elapsed_ms": {"total": elapsed, "engine": elapsed},
            "coverage_policy": policy,
            "notes": [f"Evaluated {len(options)} options via {engine_name}"],
        }

    def _decide_noul(
        self,
        start_time: float,
        state: str,
        question_id: str,
        instructions: str,
        question: Mapping[str, Any],
    ) -> dict[str, Any]:
        criteria = question.get("criteria")
        if isinstance(criteria, Mapping):
            true_text = str(criteria.get("true") or f"True: {instructions}")
            false_text = str(criteria.get("false") or f"False: Not {instructions}")
        else:
            true_text = f"True: {instructions}" if instructions else "True: Statement holds"
            false_text = f"False: Not {instructions}" if instructions else "False: Statement does not hold"

        query = f"State: {state}\nQuestion: {instructions}" if instructions else f"State: {state}"
        candidate_texts = [true_text, false_text]

        scores, engine_name, tier_path = self._score_candidates(query, candidate_texts)
        temperature = float(question.get("temperature") or self.default_temperature)
        probs = _softmax(scores, temperature)

        p_true, p_false = probs[0], probs[1]
        noul_val = round(p_true, 4)
        choice = "true" if p_true >= 0.5 else "false"
        confidence = round(max(p_true, p_false), 4)
        probabilities = {"true": noul_val, "false": round(p_false, 4)}
        margin = round(abs(p_true - p_false), 4)

        policy = os.environ.get("ELJEV_COVERAGE_POLICY", "always_abstain_v0")
        if policy == "calibrated" and confidence >= 0.60:
            status = "selected"
            exit_code = 0
        else:
            status = "needs_review"
            exit_code = 2

        elapsed = round((time.perf_counter() - start_time) * 1000.0, 3)
        return {
            "schema": SCHEMA,
            "decision_id": str(uuid4()),
            "ts": utc_timestamp(),
            "shape": "decide",
            "question_id": question_id,
            "kind": "noul",
            "criterion": instructions,
            "n_candidates": 2,
            "tier_path": tier_path,
            "engine": engine_name,
            "choice": choice,
            "choice_index": 0 if choice == "true" else 1,
            "noul": noul_val,
            "status": status,
            "exit_code": exit_code,
            "confidence": confidence,
            "probabilities": probabilities,
            "margin": margin,
            "elapsed_ms": {"total": elapsed, "engine": elapsed},
            "coverage_policy": policy,
            "notes": [f"Noul evaluation: p(true)={noul_val:.4f}"],
        }

    def _decide_score(
        self,
        start_time: float,
        state: str,
        question_id: str,
        instructions: str,
        question: Mapping[str, Any],
    ) -> dict[str, Any]:
        criteria = question.get("criteria")
        if not isinstance(criteria, Sequence) or isinstance(criteria, (str, bytes)):
            raise SystemOneError("invalid_input", "score question must provide a criteria list of levels")

        levels = [str(item) for item in criteria]
        if len(levels) < 2:
            raise SystemOneError("invalid_input", "score question must have at least 2 levels")

        query = f"State: {state}\nQuestion: {instructions}" if instructions else f"State: {state}"
        candidate_texts = [f"Level {idx + 1}: {lvl}" for idx, lvl in enumerate(levels)]

        scores, engine_name, tier_path = self._score_candidates(query, candidate_texts)
        temperature = float(question.get("temperature") or self.default_temperature)
        probs = _softmax(scores, temperature)

        top_idx = max(range(len(probs)), key=lambda i: probs[i])
        expected_score = round(sum(idx * p for idx, p in enumerate(probs)) + 1.0, 2)
        probabilities = {levels[idx]: probs[idx] for idx in range(len(levels))}
        confidence = probs[top_idx]

        policy = os.environ.get("ELJEV_COVERAGE_POLICY", "always_abstain_v0")
        if policy == "calibrated" and confidence >= 0.50:
            status = "selected"
            exit_code = 0
        else:
            status = "needs_review"
            exit_code = 2

        elapsed = round((time.perf_counter() - start_time) * 1000.0, 3)
        return {
            "schema": SCHEMA,
            "decision_id": str(uuid4()),
            "ts": utc_timestamp(),
            "shape": "decide",
            "question_id": question_id,
            "kind": "score",
            "criterion": instructions,
            "n_candidates": len(levels),
            "tier_path": tier_path,
            "engine": engine_name,
            "choice": levels[top_idx],
            "choice_index": top_idx,
            "score": expected_score,
            "status": status,
            "exit_code": exit_code,
            "confidence": confidence,
            "probabilities": probabilities,
            "elapsed_ms": {"total": elapsed, "engine": elapsed},
            "coverage_policy": policy,
            "notes": [f"Score evaluation: expected level {expected_score}"],
        }

