import unittest

from eljev.engines.cohere import parse_cohere_response


class CohereParserTests(unittest.TestCase):
    def test_recorded_score_fixture_preserves_scores_and_indices(self):
        fixture = [
            0.92933786,
            0.92888767,
            0.92817480,
            0.92784850,
            0.92752105,
            0.92191480,
            0.90535575,
            0.71985650,
            0.71985650,
            0.67542213,
            0.67542213,
            0.64027600,
            0.64027600,
            0.64027600,
            0.64027600,
            0.59793660,
        ]
        parsed = parse_cohere_response(fixture)
        self.assertEqual(len(parsed["results"]), len(fixture))
        self.assertEqual(parsed["results"][0], {"index": 0, "relevance_score": fixture[0]})
        self.assertEqual(parsed["results"][11]["relevance_score"], 0.640276)

    def test_json_response_object_is_supported(self):
        parsed = parse_cohere_response(b'{"results":[{"index":3,"relevance_score":0.9}]}')
        self.assertEqual(parsed["results"][0]["index"], 3)


if __name__ == "__main__":
    unittest.main()
