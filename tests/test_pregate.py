import unittest

from eljev.pregate import check_pregate, is_trivial
from eljev.validate import normalize_candidates


class PregateTests(unittest.TestCase):
    def test_single_candidate_is_trivial(self):
        candidates = normalize_candidates(["only"])
        result = check_pregate(candidates)
        self.assertTrue(result.trivial)
        self.assertEqual(result.reason, "single_candidate")
        self.assertTrue(is_trivial(candidates))

    def test_multiple_candidates_are_not_trivial(self):
        self.assertFalse(check_pregate(normalize_candidates(["a", "b"])).trivial)


if __name__ == "__main__":
    unittest.main()
