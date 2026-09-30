import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eljev.engines.cohere import CohereError
from eljev.engines.systemone import (
    SystemOneClient,
    SystemOneEngine,
    SystemOneError,
    _local_token_match,
    _softmax,
)


class _FakeCohere:
    def __init__(
        self,
        *,
        response=None,
        error: CohereError | None = None,
        endpoint: str = "https://example.services.ai.azure.com",
        deployment: str = "fake-deploy",
    ) -> None:
        self.response = response
        self.error = error
        self.endpoint = endpoint
        self.deployment = deployment
        self.calls: list[tuple[str, list[str], int]] = []

    def rerank(self, query: str, texts: list[str], n: int) -> dict[str, object]:
        self.calls.append((query, texts, n))
        if self.error is not None:
            raise self.error
        if callable(self.response):
            return self.response(query, texts, n)
        if self.response is not None:
            return self.response
        return {
            "results": [
                {"index": index, "relevance_score": float(1.0 / (index + 1))}
                for index in range(len(texts))
            ]
        }


class SystemOneEngineTests(unittest.TestCase):
    ENV_KEYS = (
        "ELJEV_DIR",
        "ELJEV_COVERAGE_POLICY",
        "ELJEV_CALIBRATION_PATH",
        "ELJEV_COHERE_ENDPOINT",
        "ELJEV_SYSTEMONE_URL",
        "ELJEV_AUTH",
        "EL_JEV",
        "ELJEV_PORT",
    )

    BASE_CONTRACT_KEYS = {
        "schema",
        "decision_id",
        "ts",
        "shape",
        "criterion",
        "n_candidates",
        "tier_path",
        "engine",
        "engine_version",
        "choice",
        "choice_index",
        "status",
        "exit_code",
        "raw_top_score",
        "raw_runner_up",
        "margin_raw",
        "calibrated_probability",
        "margin_calibrated",
        "calibration_version",
        "coverage_policy",
        "results",
        "elapsed_ms",
        "escalation_reason",
        "error_kind",
        "notes",
    }

    SHAPE_A_KEYS = {
        "question_id",
        "kind",
        "confidence",
        "probabilities",
        "margin",
        "noul",
        "score",
    }

    def setUp(self) -> None:
        self._environment = patch.dict(os.environ, {}, clear=False)
        self._environment.start()
        self._tempdir = tempfile.TemporaryDirectory()
        self._clear_relevant_env()
        os.environ["ELJEV_DIR"] = self._tempdir.name

    def tearDown(self) -> None:
        self._environment.stop()
        self._tempdir.cleanup()

    def _clear_relevant_env(self) -> None:
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)

    def _write_calibration(self, payload: dict[str, object]) -> Path:
        calibration_path = Path(self._tempdir.name) / "calibration.json"
        calibration_path.write_text(
            json.dumps(payload, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )
        os.environ["ELJEV_CALIBRATION_PATH"] = str(calibration_path)
        return calibration_path

    def _assert_decision_contract(self, result: dict[str, object]) -> None:
        self.assertTrue(self.BASE_CONTRACT_KEYS.issubset(result.keys()))
        self.assertTrue(self.SHAPE_A_KEYS.issubset(result.keys()))
        json.dumps(result, ensure_ascii=False, allow_nan=False)

    def test_softmax_sums_to_one(self):
        scores = [0.92, 0.75, 0.40]
        probs = _softmax(scores, temperature=0.1)
        self.assertAlmostEqual(sum(probs), 1.0, places=9)
        self.assertGreater(probs[0], probs[1])
        self.assertGreater(probs[1], probs[2])

    def test_softmax_zero_sum_handles_gracefully(self):
        probs = _softmax([0.0, 0.0], temperature=1.0)
        self.assertEqual(len(probs), 2)
        self.assertAlmostEqual(sum(probs), 1.0, places=9)

    def test_local_token_match(self):
        query = "database outage in production"
        docs = [
            "production database failure incident",
            "update company holiday schedule",
        ]
        scores = _local_token_match(query, docs)
        self.assertGreater(scores[0], scores[1])

    def test_local_heuristic_is_advisory_even_with_calibrated_policy(self):
        os.environ["ELJEV_COVERAGE_POLICY"] = "calibrated"
        self._write_calibration(
            {
                "kinds": {
                    "choice": {
                        "temperature": 1.0,
                        "threshold": 0.0,
                        "margin_threshold": 0.0,
                        "calibration_version": "choice-v1",
                    }
                }
            }
        )

        engine = SystemOneEngine(cohere=None)
        result = engine.decide(
            "reset account password needed immediately",
            {
                "id": "q-choice",
                "kind": "choice",
                "instructions": "Route intent",
                "options": [
                    {"id": "auth", "description": "password reset and account unlock"},
                    {"id": "billing", "description": "invoice and chargeback issue"},
                ],
            },
        )

        self._assert_decision_contract(result)
        self.assertEqual(result["tier_path"], ["systemone", "local_heuristic"])
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["exit_code"], 2)
        self.assertNotEqual(result["choice"], None)
        self.assertIn("uncalibrated local heuristic; advisory only", result["notes"])
        probabilities = result["probabilities"]
        self.assertIsInstance(probabilities, dict)
        self.assertAlmostEqual(sum(float(v) for v in probabilities.values()), 1.0, places=9)

    def test_default_policy_returns_needs_review_with_real_choice(self):
        engine = SystemOneEngine(
            cohere=_FakeCohere(
                response={
                    "results": [
                        {"index": 0, "relevance_score": 0.97},
                        {"index": 1, "relevance_score": 0.11},
                    ]
                }
            )
        )
        result = engine.decide(
            "duplicate refund request for invoice #123",
            {
                "id": "q-default",
                "kind": "choice",
                "instructions": "classify queue",
                "criteria": {
                    "billing_refunds": "billing and refunds",
                    "infra_ops": "infrastructure operations",
                },
            },
        )

        self._assert_decision_contract(result)
        self.assertEqual(result["choice"], "billing_refunds")
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["exit_code"], 2)

    def test_calibrated_policy_selects_with_valid_per_kind_calibration(self):
        os.environ["ELJEV_COVERAGE_POLICY"] = "calibrated"
        self._write_calibration(
            {
                "kinds": {
                    "choice": {
                        "temperature": 1.0,
                        "threshold": 0.70,
                        "margin_threshold": 0.30,
                        "calibration_version": "choice-v2",
                    },
                    "noul": {
                        "temperature": 1.0,
                        "threshold": 0.50,
                        "margin_threshold": 0.10,
                        "calibration_version": "noul-v2",
                    },
                    "score": {
                        "temperature": 1.0,
                        "threshold": 0.40,
                        "margin_threshold": 0.10,
                        "calibration_version": "score-v2",
                    },
                }
            }
        )

        engine = SystemOneEngine(
            cohere=_FakeCohere(
                response={
                    "results": [
                        {"index": 0, "relevance_score": 0.99},
                        {"index": 1, "relevance_score": 0.05},
                    ]
                }
            )
        )
        result = engine.decide(
            "billing refund requested for duplicate charge",
            {
                "id": "q-cal",
                "kind": "choice",
                "instructions": "route",
                "criteria": {
                    "billing_refunds": "billing and refunds",
                    "security": "security and auth incidents",
                },
            },
        )

        self.assertEqual(result["status"], "selected")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["calibration_version"], "choice-v2")

    def test_calibrated_choice_does_not_fallback_to_top_level_calibration(self):
        os.environ["ELJEV_COVERAGE_POLICY"] = "calibrated"
        self._write_calibration(
            {
                "temperature": 1.0,
                "threshold": 0.0,
                "margin_threshold": 0.0,
                "calibration_version": "top-level-only",
            }
        )
        engine = SystemOneEngine(
            cohere=_FakeCohere(
                response={
                    "results": [
                        {"index": 0, "relevance_score": 0.99},
                        {"index": 1, "relevance_score": 0.01},
                    ]
                }
            )
        )

        result = engine.decide(
            "duplicate billing charge",
            {
                "id": "q-no-fallback",
                "kind": "choice",
                "instructions": "route",
                "criteria": {
                    "billing_refunds": "billing and refunds",
                    "ops": "operations",
                },
            },
        )

        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["calibration_version"], "none")
        self.assertIn("no valid calibration for kind 'choice'", result["notes"])

    def test_cohere_timeout_sets_engine_error_without_heuristic_fallback(self):
        engine = SystemOneEngine(cohere=_FakeCohere(error=CohereError("timeout", "timed out")))
        result = engine.decide(
            "state",
            {
                "id": "q-timeout",
                "kind": "choice",
                "instructions": "route",
                "criteria": {"a": "alpha", "b": "beta"},
            },
        )
        self.assertEqual(result["status"], "engine_error")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["choice"], None)
        self.assertEqual(result["probabilities"], None)
        self.assertEqual(result["error_kind"], "timeout")
        self.assertNotIn("local_heuristic", result["tier_path"])

    def test_malformed_paths_return_invalid_response(self):
        scenarios = [
            _FakeCohere(error=CohereError("malformed", "bad payload")),
            _FakeCohere(
                response={
                    "results": [
                        {"index": 9, "relevance_score": 0.7},
                        {"index": 0, "relevance_score": 0.6},
                    ]
                }
            ),
        ]
        for fake in scenarios:
            with self.subTest(fake=fake):
                engine = SystemOneEngine(cohere=fake)
                result = engine.decide(
                    "state",
                    {
                        "id": "q-malformed",
                        "kind": "choice",
                        "instructions": "route",
                        "criteria": {"a": "alpha", "b": "beta"},
                    },
                )
                self.assertEqual(result["status"], "invalid_response")
                self.assertEqual(result["exit_code"], 2)
                self.assertEqual(result["error_kind"], "malformed")
                self.assertEqual(result["choice"], None)

    def test_noul_equal_scores_returns_abstain_tie(self):
        engine = SystemOneEngine(
            cohere=_FakeCohere(
                response={
                    "results": [
                        {"index": 0, "relevance_score": 0.5},
                        {"index": 1, "relevance_score": 0.5},
                    ]
                }
            )
        )
        result = engine.decide(
            "database is intermittently unavailable",
            {
                "id": "q-noul",
                "kind": "noul",
                "instructions": "Is this urgent?",
                "criteria": {"true": "urgent incident", "false": "not urgent"},
            },
        )
        self.assertEqual(result["status"], "abstain_tie")
        self.assertEqual(result["choice"], None)
        self.assertEqual(result["exit_code"], 2)

    def test_score_result_is_within_expected_level_bounds(self):
        engine = SystemOneEngine(
            cohere=_FakeCohere(
                response={
                    "results": [
                        {"index": 0, "relevance_score": 0.15},
                        {"index": 1, "relevance_score": 0.65},
                        {"index": 2, "relevance_score": 0.20},
                    ]
                }
            )
        )
        result = engine.decide(
            "service partially degraded but still operating",
            {
                "id": "q-score",
                "kind": "score",
                "instructions": "severity",
                "criteria": ["low", "medium", "high"],
            },
        )
        self.assertGreaterEqual(float(result["score"]), 1.0)
        self.assertLessEqual(float(result["score"]), 3.0)

    def test_input_caps_raise_invalid_input(self):
        valid_choice_question = {
            "id": "q-valid",
            "kind": "choice",
            "instructions": "route",
            "options": [
                {"id": "a", "description": "alpha"},
                {"id": "b", "description": "beta"},
            ],
        }

        invalid_cases = [
            ("empty_state", "", valid_choice_question),
            ("state_too_long", "x" * 100_001, valid_choice_question),
            (
                "instructions_too_long",
                "state",
                {
                    **valid_choice_question,
                    "instructions": "i" * 2_001,
                },
            ),
            (
                "choice_too_few_options",
                "state",
                {
                    **valid_choice_question,
                    "options": [{"id": "a", "description": "alpha"}],
                },
            ),
            (
                "choice_too_many_options",
                "state",
                {
                    **valid_choice_question,
                    "options": [
                        {"id": f"id-{index}", "description": f"desc-{index}"}
                        for index in range(251)
                    ],
                },
            ),
            (
                "option_too_long",
                "state",
                {
                    **valid_choice_question,
                    "options": [
                        {"id": "a", "description": "x" * 2001},
                        {"id": "b", "description": "beta"},
                    ],
                },
            ),
            (
                "duplicate_choice_ids",
                "state",
                {
                    **valid_choice_question,
                    "options": [
                        {"id": "dup", "description": "one"},
                        {"id": "dup", "description": "two"},
                    ],
                },
            ),
            (
                "score_too_few_levels",
                "state",
                {"id": "q-score1", "kind": "score", "instructions": "severity", "criteria": ["only-one"]},
            ),
            (
                "score_too_many_levels",
                "state",
                {
                    "id": "q-score2",
                    "kind": "score",
                    "instructions": "severity",
                    "criteria": [str(index) for index in range(11)],
                },
            ),
        ]

        engine = SystemOneEngine(cohere=None)
        for case_name, state, question in invalid_cases:
            with self.subTest(case=case_name):
                with self.assertRaises(SystemOneError) as raised:
                    engine.decide(state, question)
                self.assertEqual(raised.exception.error_kind, "invalid_input")

    def test_paasthrough_url_uses_systemone_client(self):
        with patch.object(SystemOneClient, "decide", return_value={"shape": "decide", "kind": "choice"}) as decide_mock:
            engine = SystemOneEngine(
                cohere=None,
                passthrough_url="http://127.0.0.1:9901",
            )
            question = {
                "id": "q-pass",
                "kind": "choice",
                "instructions": "route",
                "criteria": {"a": "alpha", "b": "beta"},
            }
            result = engine.decide({"hello": "world"}, question)
            self.assertEqual(result["shape"], "decide")
            self.assertTrue(decide_mock.called)
            called_state, called_question = decide_mock.call_args.args
            self.assertIsInstance(called_state, str)
            self.assertEqual(called_question, question)


if __name__ == "__main__":
    unittest.main()
