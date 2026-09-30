import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eljev.verdict import apply_gate, calibration_for, load_calibration


class GateTests(unittest.TestCase):
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

    @staticmethod
    def _valid_params(version: str = "v1") -> dict[str, object]:
        return {
            "temperature": 1.0,
            "threshold": 0.6,
            "margin_threshold": 0.2,
            "calibration_version": version,
        }

    def test_apply_gate_tie_precedes_untrusted(self):
        result = apply_gate(
            top=0.5,
            runner_up=0.5,
            policy="calibrated",
            params=self._valid_params(),
            trusted=False,
        )
        self.assertEqual(result["status"], "abstain_tie")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["notes"], ["top two relevance scores are exactly equal"])
        self.assertEqual(result["calibration_version"], "v1")

    def test_apply_gate_untrusted_never_selects_even_with_valid_params(self):
        result = apply_gate(
            top=0.99,
            runner_up=0.0,
            policy="calibrated",
            params=self._valid_params("cal-v2"),
            trusted=False,
        )
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["notes"], ["uncalibrated local heuristic; advisory only"])
        self.assertEqual(result["calibration_version"], "cal-v2")

    def test_apply_gate_calibrated_vacuous_margin(self):
        result = apply_gate(
            top=0.9,
            runner_up=None,
            policy="calibrated",
            params=self._valid_params(),
        )
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["exit_code"], 2)
        self.assertIn("calibrated margin term was vacuous: no runner-up exists", result["notes"])

    def test_apply_gate_calibrated_both_probability_and_margin_can_fail(self):
        result = apply_gate(
            top=0.4,
            runner_up=0.39,
            policy="calibrated",
            params={"temperature": 1.0, "threshold": 0.9, "margin_threshold": 0.2, "calibration_version": "v3"},
        )
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["exit_code"], 2)
        self.assertIn("calibrated probability below threshold", result["notes"])
        self.assertIn("calibrated margin below margin_threshold", result["notes"])
        self.assertEqual(result["calibration_version"], "v3")

    def test_apply_gate_calibrated_selects_when_both_checks_pass(self):
        result = apply_gate(
            top=0.95,
            runner_up=0.2,
            policy="calibrated",
            params={"temperature": 1.0, "threshold": 0.8, "margin_threshold": 0.3, "calibration_version": "v4"},
        )
        self.assertEqual(result["status"], "selected")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["notes"], [])
        self.assertEqual(result["calibration_version"], "v4")

    def test_apply_gate_unknown_policy_abstains(self):
        result = apply_gate(
            top=0.9,
            runner_up=0.1,
            policy="unknown_policy",
            params=self._valid_params("vX"),
        )
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["exit_code"], 2)
        self.assertIn("unknown coverage policy 'unknown_policy'; abstaining", result["notes"])
        self.assertEqual(result["calibration_version"], "vX")

    def test_calibration_for_prefers_per_kind_params(self):
        calibration = {
            "temperature": 2.0,
            "threshold": 0.1,
            "margin_threshold": 0.0,
            "calibration_version": "top-level",
            "kinds": {
                "choice": {
                    "temperature": 1.0,
                    "threshold": 0.75,
                    "margin_threshold": 0.15,
                    "calibration_version": "choice-kind",
                }
            },
        }
        params, note = calibration_for(calibration, "choice")
        self.assertIsNone(note)
        self.assertEqual(params["calibration_version"], "choice-kind")
        self.assertEqual(params["threshold"], 0.75)

    def test_calibration_for_screen_falls_back_to_top_level(self):
        calibration = self._valid_params("screen-top")
        params, note = calibration_for(calibration, "screen")
        self.assertIsNone(note)
        self.assertEqual(params["calibration_version"], "screen-top")

    def test_calibration_for_choice_does_not_fall_back_to_top_level(self):
        calibration = self._valid_params("top-only")
        params, note = calibration_for(calibration, "choice")
        self.assertIsNone(params)
        self.assertEqual(note, "no valid calibration for kind 'choice'")

    def test_calibration_for_invalid_params_returns_none_and_note(self):
        invalid_variants = [
            {"temperature": 0.0, "threshold": 0.5, "margin_threshold": 0.1, "calibration_version": "v1"},
            {"temperature": 1.0, "threshold": 1.5, "margin_threshold": 0.1, "calibration_version": "v1"},
            {"temperature": math.nan, "threshold": 0.5, "margin_threshold": 0.1, "calibration_version": "v1"},
            {"temperature": True, "threshold": 0.5, "margin_threshold": 0.1, "calibration_version": "v1"},
            {"temperature": 1.0, "threshold": 0.5, "margin_threshold": 0.1},
        ]
        for params in invalid_variants:
            with self.subTest(params=params):
                calibration = {"kinds": {"choice": params}}
                parsed, note = calibration_for(calibration, "choice")
                self.assertIsNone(parsed)
                self.assertEqual(note, "no valid calibration for kind 'choice'")

    def test_load_calibration_with_valid_file(self):
        payload = {"kinds": {"choice": self._valid_params("kind-choice")}}
        calibration_path = Path(self._tempdir.name) / "calibration.json"
        calibration_path.write_text(
            json.dumps(payload, ensure_ascii=False, allow_nan=False),
            encoding="utf-8",
        )
        os.environ["ELJEV_CALIBRATION_PATH"] = str(calibration_path)
        self.assertEqual(load_calibration(), payload)

    def test_load_calibration_invalid_or_missing_file_returns_none(self):
        missing_path = Path(self._tempdir.name) / "does-not-exist.json"
        os.environ["ELJEV_CALIBRATION_PATH"] = str(missing_path)
        self.assertIsNone(load_calibration())

        invalid_path = Path(self._tempdir.name) / "invalid.json"
        invalid_path.write_text("{", encoding="utf-8")
        os.environ["ELJEV_CALIBRATION_PATH"] = str(invalid_path)
        self.assertIsNone(load_calibration())


if __name__ == "__main__":
    unittest.main()
