import os
import tempfile
import unittest
from unittest.mock import patch

from eljev import config
from eljev.engines.cohere import CohereClient
from eljev.engines.systemone import SystemOneEngine
from eljev.validate import normalize_candidates
from eljev.verdict import verdict_from_engine


@unittest.skipUnless(
    os.environ.get("ELJEV_LIVE_TESTS") == "1" and config.cohere_endpoint(),
    "Set ELJEV_LIVE_TESTS=1 and configure ELJEV_COHERE_ENDPOINT to run live tests",
)
class LiveFoundryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.endpoint = config.cohere_endpoint()
        cls.deployment = config.cohere_deployment()
        cls.subscription = config.azure_subscription()
        cls.client = CohereClient(endpoint=cls.endpoint, deployment=cls.deployment)

    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        environment = {
            "ELJEV_DIR": self._tempdir.name,
            "ELJEV_COVERAGE_POLICY": "always_abstain_v0",
        }
        if self.subscription:
            # The temp ELJEV_DIR hides config.json, so carry the token subscription pin through.
            environment["ELJEV_AZURE_SUBSCRIPTION"] = self.subscription
        self._environment = patch.dict(os.environ, environment, clear=False)
        self._environment.start()

    def tearDown(self) -> None:
        self._environment.stop()
        self._tempdir.cleanup()

    def test_live_screen_ranks_outage_first_and_default_policy_abstains(self):
        query = "which item is most urgent to act on today"
        docs = [
            {"id": "status_report", "text": "Review routine weekly status report"},
            {"id": "prod_outage", "text": "Production outage affecting enterprise customers"},
            {"id": "readme_typo", "text": "Fix minor typo in README"},
        ]
        response = self.client.rerank(query, [item["text"] for item in docs], top_n=3)
        top_result = max(response["results"], key=lambda item: item["relevance_score"])
        self.assertEqual(top_result["index"], 1)

        verdict = verdict_from_engine(
            response,
            normalize_candidates(docs),
            policy="always_abstain_v0",
        )
        self.assertEqual(verdict["choice"], "prod_outage")
        self.assertEqual(verdict["exit_code"], 2)

    def test_live_systemone_choice_billing_refunds_default_policy_exit2(self):
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
        result = engine.decide(state, question)
        self.assertEqual(result["shape"], "decide")
        self.assertEqual(result["kind"], "choice")
        self.assertEqual(result["choice"], "billing_refunds")
        self.assertEqual(result["exit_code"], 2)

    def test_live_systemone_noul_true_default_policy_exit2(self):
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
        result = engine.decide(state, question)
        self.assertEqual(result["kind"], "noul")
        self.assertEqual(result["choice"], "true")
        self.assertEqual(result["exit_code"], 2)


if __name__ == "__main__":
    unittest.main()
