import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eljev.engines.md1 import MD1Engine, MD1Error, validate_md1_response
from eljev.engines.systemone import SystemOneError
from eljev.types import Candidate


class _FakeMD1Client:
    def __init__(self, behavior, *, endpoint: str = "https://example.services.ai.azure.com", deployment: str = "Decision-1"):
        self.behavior = behavior
        self.endpoint = endpoint
        self.deployment = deployment
        self.calls: list[tuple[object, dict[str, dict[str, object]]]] = []

    def ask(self, state, questions):  # noqa: ANN001
        self.calls.append((state, dict(questions)))
        if isinstance(self.behavior, BaseException):
            raise self.behavior
        if callable(self.behavior):
            return self.behavior(state, questions)
        return self.behavior


class MD1EngineTests(unittest.TestCase):
    ENV_KEYS = (
        "ELJEV_DIR",
        "ELJEV_COVERAGE_POLICY",
        "ELJEV_CALIBRATION_PATH",
        "ELJEV_EXTENSION_MODE",
        "ELJEV_ENGINE",
    )

    def setUp(self) -> None:
        self._environment = patch.dict(os.environ, {}, clear=False)
        self._environment.start()
        self._tempdir = tempfile.TemporaryDirectory()
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["ELJEV_DIR"] = self._tempdir.name

    def tearDown(self) -> None:
        self._environment.stop()
        self._tempdir.cleanup()

    def _write_calibration(self, payload: dict[str, object]) -> Path:
        target = Path(self._tempdir.name) / "calibration.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        os.environ["ELJEV_CALIBRATION_PATH"] = str(target)
        return target

    @staticmethod
    def _choice_question() -> dict[str, object]:
        return {
            "id": "q-choice",
            "kind": "choice",
            "instructions": "route request",
            "criteria": {
                "alpha": "billing and refunds",
                "beta": "security and auth",
            },
        }

    @staticmethod
    def _noul_question() -> dict[str, object]:
        return {
            "id": "q-noul",
            "kind": "noul",
            "instructions": "needs deep analysis?",
            "criteria": {
                "true": "deeper analysis required",
                "false": "safe to proceed directly",
            },
        }

    @staticmethod
    def _score_question() -> dict[str, object]:
        return {
            "id": "q-score",
            "kind": "score",
            "instructions": "severity",
            "criteria": ["low", "high"],
        }

    def test_validate_md1_response_rejects_invalid_shapes(self):
        choice_spec = {
            "q1": {
                "type": "choice",
                "criteria": {"alpha": "Alpha", "beta": "Beta"},
            }
        }
        noul_spec = {
            "q1": {
                "type": "noul",
                "criteria": {"true": "yes", "false": "no"},
            }
        }
        score_spec = {
            "q1": {
                "type": "score",
                "criteria": ["low", "high"],
            }
        }
        cases: list[tuple[str, object, dict[str, dict[str, object]]]] = [
            (
                "missing-question-id",
                {"answers": {"q2": {"choice": "alpha", "confidence": 0.9, "probabilities": {"alpha": 0.9, "beta": 0.1}}}},
                choice_spec,
            ),
            (
                "mismatched-choice-keys",
                {"answers": {"q1": {"choice": "alpha", "confidence": 0.9, "probabilities": {"alpha": 0.9, "gamma": 0.1}}}},
                choice_spec,
            ),
            (
                "choice-probabilities-not-sum-one",
                {"answers": {"q1": {"choice": "alpha", "confidence": 0.9, "probabilities": {"alpha": 0.8, "beta": 0.3}}}},
                choice_spec,
            ),
            (
                "choice-bool-probability",
                {"answers": {"q1": {"choice": "alpha", "confidence": 0.9, "probabilities": {"alpha": True, "beta": 0.0}}}},
                choice_spec,
            ),
            (
                "choice-nan-probability",
                {"answers": {"q1": {"choice": "alpha", "confidence": 0.9, "probabilities": {"alpha": float("nan"), "beta": 0.0}}}},
                choice_spec,
            ),
            (
                "noul-out-of-range",
                {"answers": {"q1": {"noul": 1.1}}},
                noul_spec,
            ),
            (
                "score-out-of-range",
                {"answers": {"q1": {"score": 2.5, "probabilities": {"0": 0.2, "1": 0.8}}}},
                score_spec,
            ),
        ]
        for case_name, payload, spec in cases:
            with self.subTest(case=case_name):
                decoded, error = validate_md1_response(payload, spec)
                self.assertIsNone(decoded)
                self.assertIsInstance(error, str)
                self.assertTrue(error)

    def test_decide_choice_noul_score_use_always_abstain_policy(self):
        def behavior(_state, questions):  # noqa: ANN001
            qid, question = next(iter(questions.items()))
            qtype = question["type"]
            if qtype == "choice":
                return {
                    "answers": {
                        qid: {
                            "choice": "alpha",
                            "confidence": 0.9,
                            "probabilities": {"alpha": 0.9, "beta": 0.1},
                        }
                    }
                }
            if qtype == "noul":
                return {"answers": {qid: {"noul": 0.75}}}
            return {"answers": {qid: {"score": 0.98, "probabilities": {"0": 0.02, "1": 0.98}}}}

        engine = MD1Engine(client=_FakeMD1Client(behavior))
        decisions = [
            engine.decide("state", self._choice_question()),
            engine.decide("state", self._noul_question()),
            engine.decide("state", self._score_question()),
        ]
        for result in decisions:
            self.assertEqual(result["status"], "needs_review")
            self.assertEqual(result["exit_code"], 2)

    def test_noul_exact_tie_returns_abstain_tie(self):
        engine = MD1Engine(client=_FakeMD1Client({"answers": {"q-noul": {"noul": 0.5}}}))

        result = engine.decide("state", self._noul_question())

        self.assertEqual(result["status"], "abstain_tie")
        self.assertEqual(result["choice"], None)
        self.assertEqual(result["choice_index"], None)
        self.assertEqual(result["exit_code"], 2)

    def test_score_uses_one_based_contract_and_preserves_raw_md1_score(self):
        engine = MD1Engine(
            client=_FakeMD1Client(
                {
                    "answers": {
                        "q-score": {
                            "score": 0.98,
                            "probabilities": {"0": 0.02, "1": 0.98},
                        }
                    }
                }
            )
        )

        result = engine.decide("state", self._score_question())

        self.assertAlmostEqual(float(result["raw_md1_score"]), 0.98, places=9)
        self.assertAlmostEqual(float(result["score"]), 1.98, places=9)
        self.assertEqual(set(result["probabilities"].keys()), {"low", "high"})
        self.assertAlmostEqual(float(result["probabilities"]["low"]), 0.02, places=9)
        self.assertAlmostEqual(float(result["probabilities"]["high"]), 0.98, places=9)

    def test_md1_requires_engine_scoped_calibration_when_policy_is_calibrated(self):
        os.environ["ELJEV_COVERAGE_POLICY"] = "calibrated"
        self._write_calibration(
            {
                "kinds": {
                    "choice": {
                        "temperature": 1.0,
                        "threshold": 0.6,
                        "margin_threshold": 0.2,
                        "calibration_version": "cohere-choice-only",
                    }
                }
            }
        )
        engine = MD1Engine(
            client=_FakeMD1Client(
                {
                    "answers": {
                        "q-choice": {
                            "choice": "alpha",
                            "confidence": 0.95,
                            "probabilities": {"alpha": 0.95, "beta": 0.05},
                        }
                    }
                }
            )
        )

        first = engine.decide("state", self._choice_question())
        self.assertEqual(first["status"], "needs_review")
        self.assertEqual(first["exit_code"], 2)
        self.assertIn("no valid md1 calibration for kind 'choice'", first["notes"])

        self._write_calibration(
            {
                "engines": {
                    "md1": {
                        "kinds": {
                            "choice": {
                                "temperature": 1.0,
                                "threshold": 0.7,
                                "margin_threshold": 0.3,
                                "calibration_version": "md1-choice-v1",
                            }
                        }
                    }
                }
            }
        )
        second = engine.decide("state", self._choice_question())
        self.assertEqual(second["status"], "selected")
        self.assertEqual(second["exit_code"], 0)

    def test_screen_short_circuits_single_candidate_and_ranks_multiple_candidates(self):
        def behavior(_state, questions):  # noqa: ANN001
            _qid, question = next(iter(questions.items()))
            criteria = question["criteria"]
            return {
                "answers": {
                    "screen": {
                        "choice": "b",
                        "confidence": 0.9,
                        "probabilities": {
                            candidate_id: (0.9 if candidate_id == "b" else 0.1)
                            for candidate_id in criteria
                        },
                    }
                }
            }

        engine = MD1Engine(client=_FakeMD1Client(behavior))

        trivial = engine.screen("pick one", [Candidate(id="a", text="alpha")])
        self.assertEqual(trivial["status"], "trivial")
        self.assertEqual(trivial["tier_path"], ["pregate"])

        ranked = engine.screen(
            "pick one",
            [Candidate(id="a", text="alpha"), Candidate(id="b", text="beta")],
            top_n=2,
        )
        self.assertEqual(ranked["choice"], "b")
        self.assertEqual(ranked["tier_path"], ["pregate", "md1"])
        self.assertEqual(ranked["results"][0]["id"], "b")

    def test_decide_batch_uses_single_call_and_gate_enforceable_rules(self):
        batch_client = _FakeMD1Client(
            {
                "answers": {
                    "q-choice": {
                        "choice": "alpha",
                        "confidence": 0.9,
                        "probabilities": {"alpha": 0.9, "beta": 0.1},
                    },
                    "q-noul": {"noul": 0.2},
                }
            }
        )
        batch_engine = MD1Engine(client=batch_client)
        batch = batch_engine.decide_batch(
            "state",
            {
                "q-choice": self._choice_question(),
                "q-noul": self._noul_question(),
            },
        )
        self.assertEqual(set(batch.keys()), {"q-choice", "q-noul"})
        self.assertEqual(len(batch_client.calls), 1)
        self.assertEqual(batch["q-choice"]["choice"], "alpha")

        def gate_behavior(_state, questions):  # noqa: ANN001
            self.assertEqual(set(questions.keys()), {"intent", "needs_deep_analysis", "execution_tier"})
            return {
                "answers": {
                    "intent": {
                        "choice": "code_modification",
                        "confidence": 0.95,
                        "probabilities": {
                            "code_modification": 0.95,
                            "review_audit": 0.01,
                            "investigation_search": 0.01,
                            "execution_testing": 0.02,
                            "advisory_explanation": 0.01,
                        },
                    },
                    "needs_deep_analysis": {"noul": 0.05},
                    "execution_tier": {
                        "choice": "fast_direct",
                        "confidence": 0.95,
                        "probabilities": {
                            "fast_direct": 0.95,
                            "bounded_work": 0.04,
                            "deep_system2": 0.01,
                        },
                    },
                }
            }

        os.environ["ELJEV_COVERAGE_POLICY"] = "calibrated"
        self._write_calibration(
            {
                "engines": {
                    "md1": {
                        "kinds": {
                            "choice": {
                                "temperature": 1.0,
                                "threshold": 0.7,
                                "margin_threshold": 0.2,
                                "calibration_version": "md1-choice-v1",
                            },
                            "noul": {
                                "temperature": 1.0,
                                "threshold": 0.7,
                                "margin_threshold": 0.2,
                                "calibration_version": "md1-noul-v1",
                            },
                        }
                    }
                }
            }
        )
        gate_client = _FakeMD1Client(gate_behavior)
        gate_engine = MD1Engine(client=gate_client)

        os.environ["ELJEV_EXTENSION_MODE"] = "advisory"
        advisory_result = gate_engine.gate("Implement this parser quickly", session_id="session-1")
        self.assertEqual(advisory_result["schema"], "eljev.gate/1")
        self.assertFalse(advisory_result["enforceable"])

        os.environ["ELJEV_EXTENSION_MODE"] = "enforce"
        enforce_result = gate_engine.gate("Implement this parser quickly", session_id="session-2")
        self.assertEqual(enforce_result["schema"], "eljev.gate/1")
        self.assertTrue(enforce_result["enforceable"])
        self.assertEqual(enforce_result["status"], "selected")
        self.assertEqual(enforce_result["tier"], "fast_direct")
        self.assertEqual(enforce_result["exit_code"], 0)
        self.assertAlmostEqual(float(enforce_result["needs_deep_analysis"]), 0.05, places=9)
        self.assertEqual(len(gate_client.calls), 2)
        self.assertEqual(len(gate_client.calls[0][1]), 3)
        self.assertEqual(len(gate_client.calls[1][1]), 3)

    def test_validate_md1_response_rejects_nan_score(self):
        decoded, error = validate_md1_response(
            {"answers": {"q1": {"score": float("nan"), "probabilities": {"0": 1.0, "1": 0.0}}}},
            {"q1": {"type": "score", "criteria": ["low", "high"]}},
        )
        self.assertIsNone(decoded)
        self.assertIsInstance(error, str)
        self.assertIn("score", error)
        self.assertFalse(math.isfinite(float("nan")))

    def test_auth_error_from_client_maps_to_engine_error_for_decide_screen_and_gate(self):
        engine = MD1Engine(client=_FakeMD1Client(MD1Error("auth", "token unavailable")))

        decide_result = engine.decide("state", self._choice_question())
        self.assertEqual(decide_result["status"], "engine_error")
        self.assertEqual(decide_result["error_kind"], "auth")
        self.assertEqual(decide_result["exit_code"], 2)

        screen_result = engine.screen(
            "pick one",
            [Candidate(id="alpha", text="alpha"), Candidate(id="beta", text="beta")],
        )
        self.assertEqual(screen_result["status"], "engine_error")
        self.assertEqual(screen_result["error_kind"], "auth")
        self.assertEqual(screen_result["exit_code"], 2)

        gate_result = engine.gate("Implement parser", session_id="sess-auth")
        self.assertEqual(gate_result["status"], "engine_error")
        self.assertEqual(gate_result["error_kind"], "auth")
        self.assertEqual(gate_result["exit_code"], 2)

    def test_gate_and_decide_batch_validate_state_before_endpoint_checks(self):
        engine = MD1Engine(client=_FakeMD1Client({}, endpoint=""))
        oversized_state = "x" * 100_001

        with self.assertRaises(SystemOneError) as gate_error:
            engine.gate(oversized_state, session_id="s-too-large")
        self.assertEqual(gate_error.exception.error_kind, "invalid_input")

        with self.assertRaises(SystemOneError) as batch_error:
            engine.decide_batch(oversized_state, {"q-choice": self._choice_question()})
        self.assertEqual(batch_error.exception.error_kind, "invalid_input")

    def test_decide_batch_converts_md1_errors_to_per_question_failures(self):
        engine = MD1Engine(client=_FakeMD1Client(MD1Error("auth", "token unavailable")))

        result = engine.decide_batch(
            "state",
            {
                "q-choice": self._choice_question(),
                "q-noul": self._noul_question(),
            },
        )

        self.assertEqual(set(result.keys()), {"q-choice", "q-noul"})
        for decision in result.values():
            self.assertEqual(decision["status"], "engine_error")
            self.assertEqual(decision["error_kind"], "auth")
            self.assertEqual(decision["exit_code"], 2)


if __name__ == "__main__":
    unittest.main()
