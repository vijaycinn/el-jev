import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eljev.validate import normalize_candidates
from eljev.verdict import load_calibration, verdict_from_engine


class VerdictTests(unittest.TestCase):
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

    def setUp(self):
        self._environment = patch.dict(os.environ, {}, clear=False)
        self._environment.start()
        self._tempdir = tempfile.TemporaryDirectory()
        self._clear_relevant_env()
        os.environ["ELJEV_DIR"] = self._tempdir.name
        self.candidates = normalize_candidates([{"id": "a", "text": "A"}, {"id": "b", "text": "B"}])

    def tearDown(self):
        self._environment.stop()
        self._tempdir.cleanup()

    def _clear_relevant_env(self):
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)

    def test_empty_results_fail_closed(self):
        result = verdict_from_engine({"results": []}, self.candidates)
        self.assertEqual(result["status"], "invalid_response")
        self.assertEqual(result["exit_code"], 2)

    def test_all_zero_scores_are_a_tie(self):
        result = verdict_from_engine(
            {"results": [{"index": 0, "relevance_score": 0}, {"index": 1, "relevance_score": 0}]},
            self.candidates,
        )
        self.assertEqual(result["status"], "abstain_tie")
        self.assertIsNone(result["choice"])

    def test_exact_tie_abstains(self):
        result = verdict_from_engine(
            {"results": [{"index": 0, "relevance_score": 0.75}, {"index": 1, "relevance_score": 0.75}]},
            self.candidates,
            policy="calibrated",
            calibration={
                "temperature": 1,
                "threshold": 0,
                "margin_threshold": 0,
                "calibration_version": "test",
            },
        )
        self.assertEqual(result["status"], "abstain_tie")
        self.assertEqual(result["exit_code"], 2)

    def test_out_of_range_and_nan_fail_closed(self):
        for response in (
            {"results": [{"index": 2, "relevance_score": 0.5}]},
            {"results": [{"index": 0, "relevance_score": math.nan}]},
            {"results": [{"index": 0}]},
        ):
            with self.subTest(response=response):
                self.assertEqual(
                    verdict_from_engine(response, self.candidates)["status"],
                    "invalid_response",
                )

    def test_original_index_is_preserved(self):
        result = verdict_from_engine(
            {"results": [{"index": 1, "relevance_score": 0.2}, {"index": 0, "relevance_score": 0.9}]},
            self.candidates,
        )
        self.assertEqual(result["choice"], "a")
        self.assertEqual(result["choice_index"], 0)
        self.assertEqual([item["id"] for item in result["results"]], ["a", "b"])

    def test_single_result_and_single_candidate(self):
        candidate = normalize_candidates(["only"])
        result = verdict_from_engine(
            {"results": [{"index": 0, "relevance_score": 0}]},
            candidate,
        )
        self.assertEqual(result["choice_index"], 0)
        self.assertEqual(result["status"], "needs_review")

    def test_calibrated_threshold_boundary_selects(self):
        result = verdict_from_engine(
            {"results": [{"index": 0, "relevance_score": 0.5}, {"index": 1, "relevance_score": 0.2}]},
            self.candidates,
            policy="calibrated",
            calibration={
                "temperature": 1,
                "threshold": 0.5,
                "margin_threshold": 0,
                "calibration_version": "test",
            },
        )
        self.assertEqual(result["status"], "selected")
        self.assertEqual(result["exit_code"], 0)

    def test_missing_calibration_abstains_with_note(self):
        result = verdict_from_engine(
            {"results": [{"index": 0, "relevance_score": 0.9}, {"index": 1, "relevance_score": 0.1}]},
            self.candidates,
            policy="calibrated",
            calibration=None,
        )
        self.assertEqual(result["status"], "needs_review")
        self.assertTrue(result["notes"])
        self.assertEqual(result["exit_code"], 2)

    def test_dual_gate_requires_probability_and_margin(self):
        passes = verdict_from_engine(
            {"results": [{"index": 0, "relevance_score": 0.9}, {"index": 1, "relevance_score": 0.1}]},
            self.candidates,
            policy="calibrated",
            calibration={
                "temperature": 1,
                "threshold": 0.8,
                "margin_threshold": 0.5,
                "calibration_version": "test",
            },
        )
        self.assertEqual(passes["status"], "selected")
        self.assertEqual(passes["exit_code"], 0)

        margin_fails = verdict_from_engine(
            {"results": [{"index": 0, "relevance_score": 0.9}, {"index": 1, "relevance_score": 0.89}]},
            self.candidates,
            policy="calibrated",
            calibration={
                "temperature": 1,
                "threshold": 0.8,
                "margin_threshold": 0.1,
                "calibration_version": "test",
            },
        )
        self.assertEqual(margin_fails["status"], "needs_review")
        self.assertEqual(margin_fails["exit_code"], 2)
        self.assertIn("calibrated margin below margin_threshold", margin_fails["notes"])
        self.assertNotIn("calibrated probability below threshold", margin_fails["notes"])

        both_fail = verdict_from_engine(
            {"results": [{"index": 0, "relevance_score": 0.6}, {"index": 1, "relevance_score": 0.59}]},
            self.candidates,
            policy="calibrated",
            calibration={
                "temperature": 1,
                "threshold": 0.8,
                "margin_threshold": 0.1,
                "calibration_version": "test",
            },
        )
        self.assertEqual(both_fail["status"], "needs_review")
        self.assertIn("calibrated probability below threshold", both_fail["notes"])
        self.assertIn("calibrated margin below margin_threshold", both_fail["notes"])

    def test_missing_margin_threshold_abstains(self):
        result = verdict_from_engine(
            {"results": [{"index": 0, "relevance_score": 0.9}, {"index": 1, "relevance_score": 0.1}]},
            self.candidates,
            policy="calibrated",
            calibration={
                "temperature": 1,
                "threshold": 0.5,
                "calibration_version": "test",
            },
        )
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["calibration_version"], "none")

    def test_invalid_margin_threshold_abstains(self):
        for margin_threshold in (True, math.nan, -0.01, 1.01):
            with self.subTest(margin_threshold=margin_threshold):
                result = verdict_from_engine(
                    {
                        "results": [
                            {"index": 0, "relevance_score": 0.9},
                            {"index": 1, "relevance_score": 0.1},
                        ]
                    },
                    self.candidates,
                    policy="calibrated",
                    calibration={
                        "temperature": 1,
                        "threshold": 0.5,
                        "margin_threshold": margin_threshold,
                        "calibration_version": "test",
                    },
                )
                self.assertEqual(result["status"], "needs_review")
                self.assertEqual(result["calibration_version"], "none")

    def test_single_candidate_does_not_auto_accept_vacuous_margin(self):
        candidate = normalize_candidates(["only"])
        result = verdict_from_engine(
            {"results": [{"index": 0, "relevance_score": 0.9}]},
            candidate,
            policy="calibrated",
            calibration={
                "temperature": 1,
                "threshold": 0.5,
                "margin_threshold": 0,
                "calibration_version": "test",
            },
        )
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["exit_code"], 2)
        self.assertEqual(result["margin_calibrated"], result["calibrated_probability"])
        self.assertIn("calibrated margin term was vacuous: no runner-up exists", result["notes"])

    def test_bunched_scores_abstain_with_margin_gate(self):
        scores = [0.92933786, 0.92888767, 0.92817480, 0.92784850, 0.92752105]
        candidates = normalize_candidates([f"candidate-{index}" for index in range(len(scores))])
        result = verdict_from_engine(
            {
                "results": [
                    {"index": index, "relevance_score": score}
                    for index, score in enumerate(scores)
                ]
            },
            candidates,
            policy="calibrated",
            calibration={
                "temperature": 1,
                "threshold": 0.5,
                "margin_threshold": 0.001,
                "calibration_version": "test",
            },
        )
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(result["exit_code"], 2)
        self.assertIn("calibrated margin below margin_threshold", result["notes"])

    def test_shape_b_calibrated_uses_kinds_screen_calibration_from_file(self):
        calibration_path = Path(self._tempdir.name) / "calibration.json"
        calibration_path.write_text(
            (
                '{"kinds":{"screen":{"temperature":1.0,"threshold":0.5,'
                '"margin_threshold":0.2,"calibration_version":"screen-v1"}}}'
            ),
            encoding="utf-8",
        )
        os.environ["ELJEV_CALIBRATION_PATH"] = str(calibration_path)

        calibration = load_calibration()
        result = verdict_from_engine(
            {"results": [{"index": 0, "relevance_score": 0.9}, {"index": 1, "relevance_score": 0.1}]},
            self.candidates,
            policy="calibrated",
            calibration=calibration,
        )
        self.assertEqual(result["status"], "selected")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["calibration_version"], "screen-v1")


if __name__ == "__main__":
    unittest.main()
