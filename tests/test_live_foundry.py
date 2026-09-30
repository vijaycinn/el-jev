"""Live integration tests against Azure AI Foundry Cohere deployment.

Requires:
- az login completed
- ELJEV_COHERE_ENDPOINT set or pointing to Foundry
"""

import os
import unittest
from eljev.engines.cohere import CohereClient
from eljev.engines.systemone import SystemOneEngine


class LiveFoundryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.endpoint = os.environ.get(
            "ELJEV_COHERE_ENDPOINT",
            "https://<your-resource>.services.ai.azure.com",
        )
        cls.deployment = os.environ.get(
            "ELJEV_COHERE_DEPLOYMENT", "Cohere-rerank-v4.0-pro"
        )
        cls.client = CohereClient(endpoint=cls.endpoint, deployment=cls.deployment)

    def test_live_rerank_shape_b(self):
        query = "which item is most urgent to act on today"
        docs = [
            "Review routine weekly status report",
            "Production outage affecting enterprise customers",
            "Fix minor typo in README",
        ]
        res = self.client.rerank(query, docs, top_n=3)
        self.assertIn("results", res)
        self.assertEqual(len(res["results"]), 3)
        # Production outage (index 1) should rank top
        top_result = max(res["results"], key=lambda x: x["relevance_score"])
        self.assertEqual(top_result["index"], 1)

    def test_live_systemone_choice_shape_a(self):
        engine = SystemOneEngine(cohere=self.client)
        state = "User reported billing duplicate charge of $500 on their AMEX card."
        question = {
            "id": "triage_1",
            "kind": "choice",
            "instructions": "Route this ticket to the appropriate queue",
            "criteria": {
                "billing_refunds": "Handles payment issues, charges, and refunds",
                "infra_ops": "Handles server provisioning and cloud networks",
                "marketing": "Handles promotional campaigns and newsletters",
            },
        }
        res = engine.decide(state, question)
        self.assertEqual(res["shape"], "decide")
        self.assertEqual(res["kind"], "choice")
        self.assertEqual(res["choice"], "billing_refunds")
        self.assertGreater(res["confidence"], 0.85)

    def test_live_systemone_noul_shape_a(self):
        engine = SystemOneEngine(cohere=self.client)
        state = "Incident: Primary SQL database cluster has been unreachable for 15 minutes."
        question = {
            "id": "urgency_1",
            "kind": "noul",
            "instructions": "Is this a high severity production incident?",
            "criteria": {
                "true": "Critical database downtime impacting availability",
                "false": "Low severity non-critical informational event",
            },
        }
        res = engine.decide(state, question)
        self.assertEqual(res["kind"], "noul")
        self.assertEqual(res["choice"], "true")
        self.assertGreater(res["noul"], 0.90)


if __name__ == "__main__":
    unittest.main()
