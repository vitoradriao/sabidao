import copy
import unittest
from pathlib import Path
from unittest.mock import patch

from evaluation.verify_migration_batch import evaluate_migration_batch
from evaluation import run_offline_eval


class TestMigrationBatchGate(unittest.TestCase):
    def setUp(self):
        self.preservation = {
            "batch_id": "lote-fixture",
            "status": "passed",
            "coverage": {"mapped": 1, "total": 1},
            "critical_losses": [],
            "review_status": "reviewed",
            "original_snapshot": {"commit": "a" * 40, "sha256": "b" * 64},
            "unit_results": [{"unit_id": "unidade-1", "status": "passed"}],
        }
        cases = [
            ("id", "identifier", "middle", "answerable"),
            ("paraphrase", "paraphrase", "end", "answerable"),
            ("boundary", "boundary", "end", "answerable"),
            ("ambiguous", "boundary", "middle", "ambiguous"),
            ("absent", "boundary", "end", "no_evidence"),
        ]
        rows = [
            {
                "case_id": case_id,
                "question": f"pergunta {case_id}",
                "answerability": kind,
                "provenance": {"migration": {
                    "batch_id": "lote-fixture",
                    "unit_id": "unidade-1",
                    "role": role,
                    "position": position,
                    "critical": case_id == "id",
                }},
                "review": {"human_review": "approved", "human_reviewer": "Pessoa teste", "human_reviewed_at": "2026-09-22", "divergences": []},
                "recall_at_20": 1.0 if kind == "answerable" else None,
                "citation_validity": True if kind == "answerable" else None,
                "false_absence_claim": False,
                "abstained": kind == "no_evidence",
                "clarified": kind == "ambiguous",
            }
            for case_id, role, position, kind in cases
        ]

        def report(corpus_hash):
            return {
                "started_at": "2026-09-23T12:00:00Z",
                "results": copy.deepcopy(rows),
                "runtime": {
                    "experiment_identity": {
                        "schema_version": 1,
                        "database": {
                            "status": "verified",
                            "corpus": {"sha256": corpus_hash},
                        },
                    },
                    "evaluator_schema_version": 7,
                    "metric_definitions_version": 3,
                    "metric_definitions_sha256": "same",
                    "git_commit": "same",
                    "dataset_sha256": "same",
                    "selection": {"case_ids": [row["case_id"] for row in rows]},
                },
                "model_usage": {
                    "totals": {"total_tokens": 100},
                    "estimated_cost_usd": 0.1,
                    "cost_complete": True,
                },
                "p95_latency_ms": 50,
                "metrics": {"recall_at_20": {"rate": 1.0}},
            }

        self.reference = report("corpus-before")
        self.candidate = report("corpus-after")
        self.policy = {
            "schema_version": 1,
            "batch_id": "lote-fixture",
            "frozen_at": "2026-09-23T11:00:00Z",
            "environment": {
                "disposable_database": True,
                "original_snapshot_sha256": "b" * 64,
                "reference_snapshot": "snapshot-before",
                "candidate_snapshot": "snapshot-after",
                "reference_corpus_sha256": "corpus-before",
                "candidate_corpus_sha256": "corpus-after",
            },
            "declared_changes": ["database.corpus.sha256"],
            "limits": {
                "max_total_tokens": 200,
                "max_p95_latency_ms": 100,
                "max_cost_usd": 1.0,
            },
            "non_regression_metrics": [
                {"path": "metrics.recall_at_20.rate", "direction": "higher"}
            ],
        }

    def check(self):
        return evaluate_migration_batch(
            self.preservation, self.reference, self.candidate, self.policy
        )

    def test_complete_reviewed_batch_passes(self):
        self.assertEqual(self.check()["status"], "passed")

    def test_critical_loss_fails_even_with_good_aggregate(self):
        self.preservation["critical_losses"] = [{"unit_id": "unidade-1", "kind": "sql"}]
        self.assertEqual(self.check()["status"], "failed")

    def test_missing_evidence_or_invalid_citation_fails(self):
        self.candidate["results"][0]["recall_at_20"] = 0.0
        self.candidate["results"][0]["citation_validity"] = False
        result = self.check()
        self.assertEqual(result["status"], "failed")
        self.assertIn("top20_evidence", {
            item["id"] for item in result["checks"] if item["status"] == "failed"
        })

    def test_pending_review_and_unknown_cost_are_incomplete(self):
        self.preservation["review_status"] = "pending"
        self.candidate["model_usage"]["cost_complete"] = False
        self.assertEqual(self.check()["status"], "incomplete")

    def test_approval_without_reviewer_and_date_is_incomplete(self):
        self.candidate["results"][0]["review"] = {"human_review": "approved"}
        self.assertEqual(self.check()["status"], "incomplete")

    def test_undeclared_corpus_difference_fails(self):
        self.policy["declared_changes"] = []
        self.assertEqual(self.check()["status"], "failed")

    def test_different_original_snapshot_fails(self):
        self.policy["environment"]["original_snapshot_sha256"] = "c" * 64
        self.assertEqual(self.check()["status"], "failed")

    def test_partial_unit_query_coverage_is_incomplete(self):
        self.candidate["results"] = self.candidate["results"][:2]
        self.assertEqual(self.check()["status"], "incomplete")

    def test_regression_in_lower_is_better_metric_fails(self):
        self.reference["metrics"]["false_absence_claim"] = {"rate": 0.0}
        self.candidate["metrics"]["false_absence_claim"] = {"rate": 0.1}
        self.policy["non_regression_metrics"] = [
            {"path": "metrics.false_absence_claim.rate", "direction": "lower"}
        ]
        self.assertEqual(self.check()["status"], "failed")

    def test_synthetic_cases_execute_in_offline_evaluator_without_services(self):
        fixture = (
            Path(__file__).resolve().parents[1]
            / "evaluation/datasets/migration_batch_synthetic_fixture.json"
        )
        dataset = run_offline_eval._load_dataset(fixture)
        responses = {case["question"]: case["fixture_response"] for case in dataset}

        def answer_provider(question, _scope):
            response = responses[question]
            return response["answer"], response["chunks"], response["trace"]

        with patch.object(
            run_offline_eval,
            "_database_identity",
            return_value={"status": "unknown", "reason": "fixture"},
        ):
            report = run_offline_eval.run_evaluation(
                dataset=dataset,
                dataset_name="migration-fixture",
                dry_run=True,
                limit=None,
                answer_provider=answer_provider,
            )
        self.assertEqual(report["total_cases"], 5)
        self.assertEqual(report["sample"]["human_review_pending"], 5)
        self.assertEqual(
            {row["answerability"] for row in report["results"]},
            {"answerable", "ambiguous", "no_evidence"},
        )
        self.assertTrue(all(
            row["recall_at_20"] == 1.0
            for row in report["results"] if row["answerability"] == "answerable"
        ))


if __name__ == "__main__":
    unittest.main()
