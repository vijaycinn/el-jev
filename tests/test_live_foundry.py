import os
import tempfile
import unittest
from unittest.mock import patch

from eljev import config
from eljev.engines.cohere import CohereClient
from eljev.engines.md1 import MD1Client, MD1Engine
from eljev.engines.systemone import SystemOneEngine
from eljev.types import Candidate
from eljev.validate import normalize_candidates
from eljev.verdict import verdict_from_engine


LIVE_TESTS_ENABLED = os.environ.get("ELJEV_LIVE_TESTS") == "1"
HAS_COHERE = bool(config.cohere_endpoint())
HAS_MD1 = bool(config.md1_endpoint())


@unittest.skipUnless(
    LIVE_TESTS_ENABLED and (HAS_COHERE or HAS_MD1),
    "Set ELJEV_LIVE_TESTS=1 and configure ELJEV_MD1_ENDPOINT or ELJEV_COHERE_ENDPOINT to run live tests",
)
class LiveFoundryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cohere_endpoint = config.cohere_endpoint()
        cls.cohere_deployment = config.cohere_deployment()
        cls.md1_endpoint = config.md1_endpoint()
        cls.md1_deployment = config.md1_deployment()
        cls.subscription = config.azure_subscription()
        cls.cohere_client = (
            CohereClient(endpoint=cls.cohere_endpoint, deployment=cls.cohere_deployment)
            if cls.cohere_endpoint
            else None
        )
        cls.md1_client = (
            MD1Client(endpoint=cls.md1_endpoint, deployment=cls.md1_deployment)
            if cls.md1_endpoint
            else None
        )

    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        environment = {
            "ELJEV_DIR": self._tempdir.name,
            "ELJEV_COVERAGE_POLICY": "always_abstain_v0",
        }
        if self.subscription:
            environment["ELJEV_AZURE_SUBSCRIPTION"] = self.subscription
        if self.cohere_endpoint:
            environment["ELJEV_COHERE_ENDPOINT"] = self.cohere_endpoint
            environment["ELJEV_COHERE_DEPLOYMENT"] = self.cohere_deployment
        if self.md1_endpoint:
            environment["ELJEV_MD1_ENDPOINT"] = self.md1_endpoint
            environment["ELJEV_MD1_DEPLOYMENT"] = self.md1_deployment
        self._environment = patch.dict(os.environ, environment, clear=False)
        self._environment.start()

    def tearDown(self) -> None:
        self._environment.stop()
        self._tempdir.cleanup()

    def test_live_screen_ranks_outage_first_and_default_policy_abstains(self):
        if self.cohere_client is None:
            self.skipTest("ELJEV_COHERE_ENDPOINT is not configured")
        query = "which item is most urgent to act on today"
        docs = [
            {"id": "status_report", "text": "Review routine weekly status report"},
            {"id": "prod_outage", "text": "Production outage affecting enterprise customers"},
            {"id": "readme_typo", "text": "Fix minor typo in README"},
        ]
        response = self.cohere_client.rerank(query, [item["text"] for item in docs], top_n=3)
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
        if self.cohere_client is None:
            self.skipTest("ELJEV_COHERE_ENDPOINT is not configured")
        engine = SystemOneEngine(cohere=self.cohere_client)
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
        if self.cohere_client is None:
            self.skipTest("ELJEV_COHERE_ENDPOINT is not configured")
        engine = SystemOneEngine(cohere=self.cohere_client)
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

    def test_live_md1_decide_choice_noul_and_score(self):
        if self.md1_client is None:
            self.skipTest("ELJEV_MD1_ENDPOINT is not configured")
        engine = MD1Engine(client=self.md1_client)

        choice_result = engine.decide(
            "Customer reports duplicate charge and asks for refund.",
            {
                "id": "md1-choice-live",
                "kind": "choice",
                "instructions": "Route this support request",
                "criteria": {
                    "billing": "payment and refund issues",
                    "ops": "infrastructure and operations",
                },
            },
        )
        self.assertEqual(choice_result["shape"], "decide")
        self.assertEqual(choice_result["kind"], "choice")
        self.assertIn(choice_result["choice"], {"billing", "ops", None})
        self.assertEqual(choice_result["exit_code"], 2)

        noul_result = engine.decide(
            "Primary login service is unavailable for all users.",
            {
                "id": "md1-noul-live",
                "kind": "noul",
                "instructions": "Does this require deep investigation?",
                "criteria": {
                    "true": "requires deeper incident analysis",
                    "false": "safe to handle in fast direct path",
                },
            },
        )
        self.assertEqual(noul_result["shape"], "decide")
        self.assertEqual(noul_result["kind"], "noul")
        self.assertIn(noul_result["choice"], {"true", "false", None})
        self.assertEqual(noul_result["exit_code"], 2)

        score_result = engine.decide(
            "Minor visual bug in non-critical admin page.",
            {
                "id": "md1-score-live",
                "kind": "score",
                "instructions": "Severity level",
                "criteria": ["low", "high"],
            },
        )
        self.assertEqual(score_result["shape"], "decide")
        self.assertEqual(score_result["kind"], "score")
        self.assertGreaterEqual(float(score_result["score"]), 1.0)
        self.assertLessEqual(float(score_result["score"]), 2.0)
        self.assertEqual(score_result["exit_code"], 2)

    def test_live_md1_screen_and_gate(self):
        if self.md1_client is None:
            self.skipTest("ELJEV_MD1_ENDPOINT is not configured")
        engine = MD1Engine(client=self.md1_client)

        screen_result = engine.screen(
            "Which issue is most urgent?",
            [
                Candidate(id="status", text="Weekly status report update"),
                Candidate(id="outage", text="Production outage affecting checkout"),
                Candidate(id="typo", text="Fix typo in internal README"),
            ],
            top_n=3,
        )
        self.assertEqual(screen_result["shape"], "screen")
        self.assertEqual(screen_result["tier_path"], ["pregate", "md1"])
        self.assertIn(screen_result["choice"], {"status", "outage", "typo", None})
        self.assertEqual(screen_result["exit_code"], 2)

        gate_result = engine.gate(
            "Please fix this typo in one file and run unit tests.",
            session_id="live-session-md1",
        )
        self.assertEqual(gate_result["schema"], "eljev.gate/1")
        self.assertEqual(gate_result["session_id"], "live-session-md1")
        self.assertIn("intent", gate_result["decisions"])
        self.assertIn("needs_deep_analysis", gate_result["decisions"])
        self.assertIn("execution_tier", gate_result["decisions"])
        self.assertEqual(gate_result["exit_code"], 2)


if __name__ == "__main__":
    unittest.main()
