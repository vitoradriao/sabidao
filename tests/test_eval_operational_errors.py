import unittest

from evaluation import run_offline_eval as evaluator


class TestOperationalErrorOutcomes(unittest.TestCase):
    def test_provider_error_no_answer_is_not_correct_abstention(self):
        failure = {
            "answerability": "no_evidence",
            "abstained": True,
            "clarified": False,
            "operational_failure": evaluator._operational_failure({
                "response_state": "provider_error",
                "semantic_grounding": {"status": "skipped", "reason": "disabled"},
            }),
        }
        failure["outcome"] = evaluator._case_outcome(failure)
        valid = {**failure, "operational_failure": False}
        valid["outcome"] = evaluator._case_outcome(valid)
        outcomes = evaluator._summarize_outcomes([failure, valid])
        self.assertEqual(failure["outcome"], "operational_failure")
        self.assertEqual(outcomes["correct_abstentions"]["count"], 1)
        self.assertEqual(outcomes["correct_abstentions"]["denominator"], 2)
        self.assertEqual(outcomes["correct_abstentions"]["rate"], 0.5)

    def test_insufficient_evidence_is_distinct_from_operational_states(self):
        self.assertFalse(evaluator._operational_failure({
            "response_state": "insufficient_evidence",
        }))
        for state in ("provider_error", "operational_error"):
            with self.subTest(state=state):
                self.assertTrue(evaluator._operational_failure({"response_state": state}))


if __name__ == "__main__":
    unittest.main()
