import unittest

from eljev.validate import (
    InputValidationError,
    MAX_CHARS_PER_CANDIDATE,
    MAX_TOTAL_CHARS,
    normalize_candidates,
    validate_criterion,
)


class ValidateTests(unittest.TestCase):
    def test_candidate_count_boundaries(self):
        self.assertEqual(len(normalize_candidates(["x"])), 1)
        self.assertEqual(len(normalize_candidates(["x"] * 250)), 250)
        with self.assertRaises(InputValidationError):
            normalize_candidates([])
        with self.assertRaises(InputValidationError):
            normalize_candidates(["x"] * 251)

    def test_character_caps_boundaries(self):
        self.assertEqual(len(normalize_candidates(["x" * MAX_CHARS_PER_CANDIDATE])), 1)
        with self.assertRaises(InputValidationError):
            normalize_candidates(["x" * (MAX_CHARS_PER_CANDIDATE + 1)])
        self.assertEqual(
            sum(len(candidate.text) for candidate in normalize_candidates(["x" * 2000] * 50)),
            MAX_TOTAL_CHARS,
        )
        with self.assertRaises(InputValidationError):
            normalize_candidates(["x" * 2000] * 50 + ["x"])

    def test_duplicate_empty_and_criterion_validation(self):
        with self.assertRaises(InputValidationError):
            normalize_candidates([{"id": "a", "text": "one"}, {"id": "a", "text": "two"}])
        with self.assertRaises(InputValidationError):
            normalize_candidates([""])
        with self.assertRaises(InputValidationError):
            normalize_candidates(["  "])
        with self.assertRaises(InputValidationError):
            validate_criterion("")
        with self.assertRaises(InputValidationError):
            validate_criterion(" \t")

    def test_plain_strings_receive_stable_ids(self):
        self.assertEqual(
            [candidate.to_dict() for candidate in normalize_candidates(["a", "b"])],
            [{"id": "c0", "text": "a"}, {"id": "c1", "text": "b"}],
        )


if __name__ == "__main__":
    unittest.main()
