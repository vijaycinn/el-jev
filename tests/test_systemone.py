import math
import unittest

from eljev.engines.systemone import SystemOneEngine, _softmax, _local_token_match


class SystemOneEngineTests(unittest.TestCase):
    def test_softmax_sums_to_one(self):
        scores = [0.92, 0.75, 0.40]
        probs = _softmax(scores, temperature=0.1)
        self.assertAlmostEqual(sum(probs), 1.0, places=3)
        self.assertGreater(probs[0], probs[1])
        self.assertGreater(probs[1], probs[2])

    def test_softmax_zero_sum_handles_gracefully(self):
        probs = _softmax([0.0, 0.0], temperature=1.0)
        self.assertEqual(len(probs), 2)
        self.assertAlmostEqual(sum(probs), 1.0, places=3)

    def test_local_token_match(self):
        query = "database outage in production"
        docs = [
            "production database failure incident",
            "update company holiday schedule",
        ]
        scores = _local_token_match(query, docs)
        self.assertGreater(scores[0], scores[1])

    def test_decide_choice_heuristic(self):
        engine = SystemOneEngine(cohere=None)
        state = "The user is asking how to reset their account password."
        question = {
            "id": "q1",
            "kind": "choice",
            "instructions": "Which department should handle this?",
            "options": [
                {"id": "auth", "description": "Account password and login issues"},
                {"id": "billing", "description": "Credit card and invoice questions"},
            ],
        }
        res = engine.decide(state, question)
        self.assertEqual(res["shape"], "decide")
        self.assertEqual(res["kind"], "choice")
        self.assertEqual(res["choice"], "auth")
        self.assertGreater(res["confidence"], 0.5)
        self.assertIn("auth", res["probabilities"])
        self.assertIn("billing", res["probabilities"])

    def test_decide_choice_with_criteria_dict(self):
        engine = SystemOneEngine(cohere=None)
        state = "Refund needed for transaction TX-100."
        question = {
            "id": "q2",
            "kind": "choice",
            "instructions": "Classify intent",
            "criteria": {
                "refund": "Customer requesting money back",
                "sales": "Prospective buyer questions",
            },
        }
        res = engine.decide(state, question)
        self.assertEqual(res["choice"], "refund")
        self.assertGreater(res["confidence"], 0.5)

    def test_decide_noul_heuristic(self):
        engine = SystemOneEngine(cohere=None)
        state = "CRITICAL: The server is down and throwing 500 errors across all routes."
        question = {
            "id": "q3",
            "kind": "noul",
            "instructions": "Is this an urgent server incident?",
            "criteria": {
                "true": "Critical server outage incident requiring immediate response",
                "false": "Low priority non-urgent cosmetic issue",
            },
        }
        res = engine.decide(state, question)
        self.assertEqual(res["shape"], "decide")
        self.assertEqual(res["kind"], "noul")
        self.assertEqual(res["choice"], "true")
        self.assertGreater(res["noul"], 0.5)

    def test_decide_score_heuristic(self):
        engine = SystemOneEngine(cohere=None)
        state = "Major catastrophic failure in core payment processing"
        question = {
            "id": "q4",
            "kind": "score",
            "instructions": "Rate severity",
            "criteria": ["low", "medium", "critical payment failure"],
        }
        res = engine.decide(state, question)
        self.assertEqual(res["kind"], "score")
        self.assertIn("score", res)
        self.assertEqual(res["choice"], "critical payment failure")


if __name__ == "__main__":
    unittest.main()
