import unittest
from pathlib import Path

from evaluation import build_dataset, run_offline_eval


ROOT_DIR = Path(__file__).resolve().parents[1]
SYNTHETIC_FIXTURE = (
    ROOT_DIR / "evaluation" / "datasets" / "evaluator_synthetic_fixture.json"
)
LEGACY_DATASET = ROOT_DIR / "evaluation" / "datasets" / "maxpedido_eval_dataset.json"


class TestOfflineEvaluator(unittest.TestCase):
    def test_wrong_answer_fails_factual_metric_even_with_valid_citation(self):
        summary = self._run_synthetic_fixture()
        results = {result["case_id"]: result for result in summary["results"]}

        correct = results["correct-answer"]
        wrong = results["wrong-answer-valid-citation"]

        self.assertTrue(correct["factual_correctness"])
        self.assertEqual(correct["score"], 1.0)
        self.assertTrue(wrong["behavior_match"])
        self.assertTrue(wrong["citation_validity"])
        self.assertFalse(wrong["factual_correctness"])
        self.assertLess(wrong["score"], 1.0)

    def test_abstentions_are_distinguished_and_do_not_pass_citation(self):
        summary = self._run_synthetic_fixture()
        results = {result["case_id"]: result for result in summary["results"]}

        correct = results["correct-abstention"]
        incorrect = results["incorrect-abstention"]

        self.assertTrue(correct["behavior_match"])
        self.assertIsNone(correct["factual_correctness"])
        self.assertIsNone(correct["citation_validity"])
        self.assertFalse(incorrect["behavior_match"])
        self.assertFalse(incorrect["factual_correctness"])
        self.assertIsNone(incorrect["citation_validity"])

    def test_report_exposes_denominators_and_unevaluated_cases(self):
        summary = self._run_synthetic_fixture()

        factual = summary["metrics"]["factual_correctness"]
        citations = summary["metrics"]["citation_validity"]
        recall = summary["metrics"]["recall_at_k"]
        self.assertEqual(factual["evaluated"], 3)
        self.assertEqual(factual["not_evaluated"], 1)
        self.assertEqual(citations["evaluated"], 2)
        self.assertEqual(citations["not_evaluated"], 2)
        self.assertEqual(recall, summary["metrics"]["retrieval_relevance"])
        self.assertEqual(summary["metrics"]["false_abstention"]["evaluated"], 3)
        self.assertEqual(summary["metrics"]["false_absence_claim"]["evaluated"], 3)
        self.assertIsNotNone(summary["avg_latency_ms"])
        self.assertIsNotNone(summary["p95_latency_ms"])
        self.assertIn("factual_correctness", summary["metric_definitions"])
        self.assertEqual(summary["score_evaluated"], 4)

    def test_false_absence_claim_is_measured_only_for_expected_answers(self):
        evaluation = run_offline_eval._evaluate_response(
            case={
                "expected_behavior": "exact_answer",
                "expected_intent": "general",
            },
            answer="Esse parametro nao consta na documentacao.",
            chunks=[],
            trace={"query_plan": {"intent": "general"}, "abstained": False},
        )

        self.assertFalse(evaluation["false_abstention"])
        self.assertTrue(evaluation["false_absence_claim"])

    def test_legacy_dataset_remains_loadable_without_claiming_factual_success(self):
        legacy_cases = run_offline_eval._load_dataset(LEGACY_DATASET)
        case = legacy_cases[0]

        evaluation = run_offline_eval._evaluate_response(
            case=case,
            answer="Uma resposta qualquer com fonte. [Fonte: legado.md]",
            chunks=[{"filename": "legado.md", "content": "conteudo"}],
            trace={
                "query_plan": {"intent": case["expected_intent"]},
                "abstained": False,
                "cited_files": ["legado.md"],
                "grounding_errors": [],
            },
        )

        self.assertIsNone(evaluation["factual_correctness"])
        self.assertIsNone(evaluation["citation_validity"])
        self.assertIsNone(evaluation["score"])

    def test_dataset_builder_preserves_v2_reference_fields(self):
        fixture_case = run_offline_eval._load_dataset(SYNTHETIC_FIXTURE)[0]

        normalized = build_dataset._normalize_case(fixture_case, 1, "fixture")

        self.assertEqual(normalized["expected_facts"], fixture_case["expected_facts"])
        self.assertEqual(
            normalized["reference_evidence"],
            fixture_case["reference_evidence"],
        )

    def _run_synthetic_fixture(self):
        dataset = run_offline_eval._load_dataset(SYNTHETIC_FIXTURE)
        responses = {
            case["question"]: case["fixture_response"]
            for case in dataset
        }

        def fixture_provider(question, _scope):
            response = responses[question]
            return response["answer"], response["chunks"], response["trace"]

        return run_offline_eval.run_evaluation(
            dataset=dataset,
            dataset_name="synthetic",
            dry_run=True,
            limit=None,
            answer_provider=fixture_provider,
        )


if __name__ == "__main__":
    unittest.main()
