import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from evaluation import build_dataset, run_offline_eval


ROOT_DIR = Path(__file__).resolve().parents[1]
SYNTHETIC_FIXTURE = (
    ROOT_DIR / "evaluation" / "datasets" / "evaluator_synthetic_fixture.json"
)
BASELINE_DATASET = ROOT_DIR / "evaluation" / "datasets" / "maxpedido_eval_dataset.json"


class TestOfflineEvaluator(unittest.TestCase):
    def test_git_commit_accepts_explicit_runtime_identity(self):
        with patch.dict(
            run_offline_eval.os.environ,
            {"BENCHMARK_GIT_COMMIT": "commit-congelado"},
        ):
            self.assertEqual(run_offline_eval._git_commit(), "commit-congelado")

    @staticmethod
    def _verified_database_identity(
        *,
        corpus_sha256: str = "corpus-a",
        feedback_sha256: str = "feedback-a",
    ):
        configured = run_offline_eval.rag.get_embedding_index_identity()
        return {
            "status": "verified",
            "reason": None,
            "schema_version": 1,
            "corpus": {
                "sha256": corpus_sha256,
                "document_count": 2,
                "section_count": 3,
                "chunk_count": 4,
            },
            "feedback": {
                "sha256": feedback_sha256,
                "item_count": 1,
                "chunk_count": 1,
            },
            "persisted_vector_identities": [
                {"index_scope": scope, **configured}
                for scope in ("feedback", "corpus", "sections")
            ],
        }

    def test_experiment_identity_tracks_challenger_prompt_corpus_and_feedback(self):
        database = self._verified_database_identity()
        with patch.object(run_offline_eval, "_database_identity", return_value=database):
            baseline = run_offline_eval._experiment_identity({"policy": "v1"})
            repeated = run_offline_eval._experiment_identity({"policy": "v1"})

            with patch.object(
                run_offline_eval.config,
                "RAG_GLOBAL_CHALLENGER_COUNT",
                run_offline_eval.config.RAG_GLOBAL_CHALLENGER_COUNT + 1,
            ):
                challenger_changed = run_offline_eval._experiment_identity({"policy": "v1"})

            with patch.object(
                run_offline_eval.config,
                "SYSTEM_PROMPT",
                run_offline_eval.config.SYSTEM_PROMPT + "\nregra nova",
            ):
                prompt_changed = run_offline_eval._experiment_identity({"policy": "v1"})

        with patch.object(
            run_offline_eval,
            "_database_identity",
            return_value=self._verified_database_identity(corpus_sha256="corpus-b"),
        ):
            corpus_changed = run_offline_eval._experiment_identity({"policy": "v1"})
        with patch.object(
            run_offline_eval,
            "_database_identity",
            return_value=self._verified_database_identity(feedback_sha256="feedback-b"),
        ):
            feedback_changed = run_offline_eval._experiment_identity({"policy": "v1"})

        self.assertEqual(baseline["fingerprint_sha256"], repeated["fingerprint_sha256"])
        self.assertIn("CONTEXTUAL_RETRIEVAL_ENABLED", baseline["rag_config"])
        self.assertIn("routing_policy", baseline["prompts_and_policies"])
        self.assertIn("response_policy", baseline["prompts_and_policies"])
        for changed in (
            challenger_changed,
            prompt_changed,
            corpus_changed,
            feedback_changed,
        ):
            self.assertNotEqual(
                baseline["fingerprint_sha256"],
                changed["fingerprint_sha256"],
            )

    def test_database_identity_normalizes_persisted_vector_order(self):
        configured = run_offline_eval.rag.get_embedding_index_identity()
        row = {
            "schema_version": 1,
            "corpus_sha256": "corpus",
            "corpus_document_count": 1,
            "corpus_section_count": 2,
            "corpus_chunk_count": 3,
            "feedback_sha256": "feedback",
            "feedback_item_count": 1,
            "feedback_chunk_count": 1,
            "vector_index_identities": [
                {"index_scope": scope, **configured}
                for scope in ("sections", "feedback", "corpus")
            ],
        }
        with (
            patch.dict("os.environ", {"DATABASE_URL": "postgresql://fixture"}),
            patch.object(run_offline_eval.rag, "supabase_rpc", return_value=[row]),
        ):
            identity = run_offline_eval._database_identity()

        self.assertEqual(
            [item["index_scope"] for item in identity["persisted_vector_identities"]],
            ["corpus", "feedback", "sections"],
        )
        self.assertNotIn("content", json.dumps(identity))

    def test_runtime_comparison_requires_declared_differences(self):
        database = self._verified_database_identity()
        with patch.object(run_offline_eval, "_database_identity", return_value=database):
            reference_identity = run_offline_eval._experiment_identity({"policy": "v1"})
            with patch.object(
                run_offline_eval.config,
                "RAG_GLOBAL_CHALLENGER_COUNT",
                run_offline_eval.config.RAG_GLOBAL_CHALLENGER_COUNT + 1,
            ):
                current_identity = run_offline_eval._experiment_identity({"policy": "v1"})

        reference = {"experiment_identity": reference_identity}
        current = {"experiment_identity": current_identity}
        incompatible = run_offline_eval._compare_runtime_identities(current, reference)
        declared = run_offline_eval._compare_runtime_identities(
            current,
            reference,
            experimental_variables=["rag_config.RAG_GLOBAL_CHALLENGER_COUNT"],
        )

        self.assertEqual(incompatible["status"], "incompatible")
        self.assertFalse(incompatible["compatible"])
        self.assertEqual(declared["status"], "compatible_with_declared_changes")
        self.assertTrue(declared["compatible"])
        self.assertEqual(len(declared["differences"]), 1)

    def test_runtime_comparison_does_not_accept_unknown_database_identity(self):
        unknown = {
            "experiment_identity": {
                "database": {"status": "unknown"},
                "fingerprint_sha256": "same",
            }
        }

        comparison = run_offline_eval._compare_runtime_identities(unknown, unknown)

        self.assertEqual(comparison["status"], "incomplete")
        self.assertFalse(comparison["compatible"])

    def test_baseline_requires_every_criterion_and_full_coverage(self):
        config = json.loads((ROOT_DIR / "evaluation/baseline_config.json").read_text(encoding="utf-8"))
        holdout = {
            "sample_size": 10,
            "outcomes": {"population": {"answerable": 6, "ambiguous": 2, "no_evidence": 2}},
        }
        for rule in config["non_regression"]:
            target = holdout
            parts = rule["path"].split(".")
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = rule["value"]

        def evaluate(summary, expected=10):
            return run_offline_eval._evaluate_non_regression(
                summary, config, expected_holdout_cases=expected
            )

        self.assertEqual(evaluate(holdout)["status"], "passed")
        for rules in ([], [None]):
            with self.subTest(rules=rules):
                result = run_offline_eval._evaluate_non_regression(
                    holdout, {"non_regression": rules}, expected_holdout_cases=10
                )
                self.assertEqual(result["status"], "incomplete")
        self.assertEqual(evaluate(holdout, expected=11)["status"], "incomplete")
        self.assertEqual(evaluate(None)["status"], "incomplete")
        partial = {"outcomes": {"unsupported_answers": {"rate": 0}}}
        result = evaluate(partial)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(len(result["missing_checks"]), 6)

        holdout["outcomes"]["population"]["ambiguous"] = 0
        result = evaluate(holdout)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["coverage"]["missing_populations"], ["ambiguous"])
        holdout["outcomes"]["population"]["ambiguous"] = 2
        holdout["outcomes"]["necessary_clarifications"] = run_offline_eval._rate_summary(0, 0)
        self.assertEqual(evaluate(holdout)["status"], "incomplete")
        holdout["outcomes"]["unsupported_answers"]["rate"] = 1
        result = evaluate(holdout)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["complete"])
        self.assertTrue(result["critical_failures"])
        self.assertTrue(result["missing_checks"])
        holdout["outcomes"]["unsupported_answers"]["rate"] = 0
        holdout["outcomes"]["necessary_clarifications"]["rate"] = 0
        result = evaluate(holdout)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["critical_failures"], [])

    def test_cli_gate_preserves_report_and_exploratory_exit(self):
        summary = self._run_synthetic_fixture()
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            for status, gated, expected_exit in (
                ("passed", True, 0), ("failed", True, 1),
                ("incomplete", True, 1), (None, True, 1),
                ("failed", False, 0), ("incomplete", False, 0),
            ):
                with self.subTest(status=status, gated=gated):
                    summary["non_regression"] = {"status": status} if status else None
                    argv = ["run_offline_eval", "--dry-run", "--output-report", str(report)]
                    if gated:
                        argv.append("--gate")
                    with patch("sys.argv", argv), patch("builtins.print"), patch.object(
                        run_offline_eval, "run_evaluation", return_value=summary
                    ):
                        self.assertEqual(run_offline_eval.main(), expected_exit)
                    saved = json.loads(report.read_text(encoding="utf-8"))
                    self.assertEqual(saved["non_regression"], summary["non_regression"])

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
        recall_10 = summary["metrics"]["recall_at_10"]
        recall_20 = summary["metrics"]["recall_at_20"]
        self.assertEqual(factual["evaluated"], 3)
        self.assertEqual(factual["not_evaluated"], 1)
        self.assertEqual(citations["evaluated"], 2)
        self.assertEqual(citations["not_evaluated"], 2)
        self.assertEqual(recall_10["evaluated"], 3)
        self.assertEqual(recall_20["evaluated"], 3)
        self.assertEqual(recall_10["rate"], recall_20["rate"])
        self.assertEqual(summary["metrics"]["false_abstention"]["evaluated"], 3)
        self.assertEqual(summary["metrics"]["false_absence_claim"]["evaluated"], 3)
        self.assertNotIn("passed", summary["metrics"]["false_abstention"])
        self.assertIn("occurrences", summary["metrics"]["false_abstention"])
        self.assertNotIn("passed", summary["metrics"]["false_absence_claim"])
        self.assertIn("occurrences", summary["metrics"]["false_absence_claim"])
        self.assertIsNotNone(summary["avg_latency_ms"])
        self.assertIsNotNone(summary["p50_latency_ms"])
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

    def test_false_absence_claim_handles_negation_and_paraphrases(self):
        cases = {
            "Esse parametro nao foi encontrado.": True,
            "Esse parametro nao esta documentado.": True,
            "Esse parametro e inexistente.": True,
            "Esse parametro nao e inexistente.": False,
        }

        for answer, expected in cases.items():
            with self.subTest(answer=answer):
                self.assertEqual(
                    run_offline_eval._false_absence_claim(
                        "exact_answer",
                        False,
                        answer,
                    ),
                    expected,
                )

    def test_legacy_dataset_remains_loadable_without_claiming_factual_success(self):
        case = {
            "expected_behavior": "exact_answer",
            "expected_intent": "general",
        }

        evaluation = run_offline_eval._evaluate_response(
            case=case,
            answer="Uma resposta qualquer com fonte. [Fonte: legado.md]",
            chunks=[{"filename": "legado.md", "content": "conteudo"}],
            trace={
                "query_plan": {"intent": "general"},
                "abstained": False,
                "cited_files": ["legado.md"],
                "grounding_errors": [],
            },
        )

        self.assertIsNone(evaluation["factual_correctness"])
        self.assertIsNone(evaluation["citation_validity"])
        self.assertIsNone(evaluation["score"])

    def test_dataset_builder_preserves_baseline_fields(self):
        fixture_case = run_offline_eval._load_dataset(SYNTHETIC_FIXTURE)[0]
        fixture_case.update(
            {
                "split": "holdout",
                "answerability": "answerable",
                "forbidden_facts": ["valor N"],
                "provenance": {"source": "fixture"},
                "review": {"status": "reviewed"},
                "conversation_history": [{"role": "user", "content": "contexto"}],
            }
        )

        normalized = build_dataset._normalize_case(fixture_case, 1, "fixture")

        self.assertEqual(normalized["expected_facts"], fixture_case["expected_facts"])
        self.assertEqual(
            normalized["reference_evidence"],
            fixture_case["reference_evidence"],
        )
        for field in (
            "split",
            "answerability",
            "forbidden_facts",
            "provenance",
            "review",
            "conversation_history",
        ):
            self.assertEqual(normalized[field], fixture_case[field])

    def test_baseline_has_30_traceable_cases_and_is_split(self):
        dataset = run_offline_eval._load_dataset(BASELINE_DATASET)

        self.assertEqual(len(dataset), 30)
        self.assertEqual(sum(case["split"] == "development" for case in dataset), 20)
        self.assertEqual(sum(case["split"] == "holdout" for case in dataset), 10)
        self.assertTrue(all(case.get("provenance") for case in dataset))
        self.assertTrue(all(case.get("review", {}).get("reviewer") for case in dataset))
        self.assertTrue(
            all(
                case.get("review", {}).get("human_review") == "approved"
                for case in dataset
            )
        )
        self.assertTrue(
            all(
                case.get("review", {}).get("human_reviewer") == "vitoradriao"
                for case in dataset
            )
        )
        self.assertEqual(
            {case["answerability"] for case in dataset},
            {"answerable", "ambiguous", "no_evidence"},
        )

    def test_ranked_retrieval_metrics_measure_two_cutoffs_and_ndcg(self):
        evidence = [
            {"source": "a.md", "contains": ["alfa"]},
            {"source": "b.md", "contains": ["beta"]},
        ]
        chunks = [
            {"filename": "a.md", "content": "alfa"},
            *[
                {"filename": f"noise-{index}.md", "content": "ruido"}
                for index in range(1, 15)
            ],
            {"filename": "b.md", "content": "beta"},
        ]

        metrics, details = run_offline_eval._retrieval_metrics(chunks, evidence)

        self.assertEqual(metrics["recall_at_10"], 0.5)
        self.assertEqual(metrics["recall_at_20"], 1.0)
        self.assertGreater(metrics["ndcg_at_10"], 0.0)
        self.assertLess(metrics["ndcg_at_10"], 1.0)
        self.assertEqual(details["retrieved_depth"], 16)

    def test_report_separates_decision_outcomes_and_evaluates_holdout_gates(self):
        dataset = [
            {
                "id": "answer",
                "split": "holdout",
                "question": "q1",
                "answerability": "answerable",
                "expected_behavior": "exact_answer",
                "expected_intent": "general",
                "expected_facts": ["fato"],
            },
            {
                "id": "clarify",
                "split": "holdout",
                "question": "q2",
                "answerability": "ambiguous",
                "expected_behavior": "clarify",
                "expected_intent": "general",
            },
            {
                "id": "abstain",
                "split": "holdout",
                "question": "q3",
                "answerability": "no_evidence",
                "expected_behavior": "no_answer",
                "expected_intent": "general",
            },
        ]
        responses = {
            "q1": ("fato", [], {"query_plan": {"intent": "general"}}),
            "q2": (
                "Pode informar qual pedido?",
                [],
                {"query_plan": {"intent": "general"}},
            ),
            "q3": (
                "sem evidencia",
                [],
                {"query_plan": {"intent": "general"}, "abstained": True},
            ),
        }
        baseline_config = {
            "non_regression": [
                {
                    "id": "answers",
                    "path": "outcomes.correct_answers.rate",
                    "operator": ">=",
                    "value": 1.0,
                    "critical": True,
                }
            ]
        }

        summary = run_offline_eval.run_evaluation(
            dataset=dataset,
            dataset_name="decisions",
            dry_run=True,
            limit=None,
            answer_provider=lambda question, _scope: responses[question],
            baseline_config=baseline_config,
        )

        self.assertEqual(summary["outcomes"]["correct_answers"]["count"], 1)
        self.assertEqual(
            summary["outcomes"]["necessary_clarifications"]["count"],
            1,
        )
        self.assertEqual(summary["outcomes"]["correct_abstentions"]["count"], 1)
        self.assertEqual(summary["non_regression"]["status"], "passed")
        self.assertIn("git_commit", summary["runtime"])
        self.assertIn("model_config", summary["runtime"])
        self.assertIn("embedding_index_identity", summary["runtime"])

        for split, limit, status in (
            ("holdout", 1, "incomplete"),
            ("development", None, "incomplete"),
            ("holdout", None, "passed"),
            ("all", 3, "passed"),
        ):
            with self.subTest(split=split, limit=limit):
                partial = run_offline_eval.run_evaluation(
                    dataset=dataset + [{**dataset[0], "id": "dev", "split": "development"}],
                    dataset_name="decisions", dry_run=True, limit=limit, split=split,
                    answer_provider=lambda question, _scope: responses[question],
                    baseline_config=baseline_config,
                )
                self.assertEqual(partial["non_regression"]["status"], status)
                self.assertEqual(partial["non_regression"]["coverage"]["expected_holdout_cases"], 3)

    def test_runner_passes_follow_up_history_to_rag(self):
        history = [{"role": "user", "content": "contexto anterior"}]
        dataset = [
            {
                "id": "follow-up",
                "question": "E depois?",
                "conversation_history": history,
                "expected_behavior": "no_answer",
                "expected_intent": "general",
            }
        ]

        with patch.object(
            run_offline_eval.rag,
            "ask",
            return_value=("sem evidencia", [], {"abstained": True}),
        ) as ask:
            run_offline_eval.run_evaluation(
                dataset=dataset,
                dataset_name="follow-up",
                dry_run=True,
                limit=None,
            )

        self.assertEqual(ask.call_args.kwargs["conversation_history"], history)
        self.assertEqual(ask.call_args.kwargs["platform"], "offline_eval")

    def test_model_usage_aggregates_every_call_and_cost(self):
        calls = [
            {
                "usage": {
                    "input_tokens": 10,
                    "cached_input_tokens": 2,
                    "output_tokens": 3,
                    "reasoning_tokens": 1,
                    "total_tokens": 16,
                },
                "estimated_cost_usd": 0.01,
            },
            {
                "usage": {
                    "input_tokens": 20,
                    "cached_input_tokens": 0,
                    "output_tokens": 4,
                    "reasoning_tokens": 2,
                    "total_tokens": 26,
                },
                "estimated_cost_usd": 0.02,
            },
        ]

        summary = run_offline_eval._summarize_model_usage(
            [{"trace": {"model_calls": calls}}]
        )

        self.assertEqual(summary["call_count"], 2)
        self.assertEqual(summary["totals"]["total_tokens"], 42)
        self.assertEqual(summary["estimated_cost_usd"], 0.03)
        self.assertTrue(summary["cost_complete"])

    def test_model_usage_marks_unpriced_embedding_call_as_incomplete(self):
        summary = run_offline_eval._summarize_model_usage(
            [
                {
                    "trace": {
                        "model_calls": [],
                        "external_calls": [
                            {
                                "stage": "query_embedding",
                                "usage": None,
                                "estimated_cost_usd": None,
                            }
                        ],
                    }
                }
            ]
        )

        self.assertEqual(summary["call_count"], 1)
        self.assertEqual(summary["external_call_count"], 1)
        self.assertEqual(summary["calls_without_usage"], 1)
        self.assertFalse(summary["cost_complete"])

    def test_query_embedding_records_provider_call_and_cache_hit(self):
        calls = []
        token = run_offline_eval.rag._request_external_calls.set(calls)
        try:
            with (
                patch.object(
                    run_offline_eval.rag,
                    "_query_embedding_cache",
                    run_offline_eval.rag._TTLCache(maxsize=2, ttl=60),
                ),
                patch.object(
                    run_offline_eval.rag,
                    "create_query_embedding",
                    return_value=[0.1],
                ) as create_embedding,
            ):
                run_offline_eval.rag._get_cached_query_embedding("consulta unica")
                run_offline_eval.rag._get_cached_query_embedding("consulta unica")
        finally:
            run_offline_eval.rag._request_external_calls.reset(token)

        self.assertEqual(create_embedding.call_count, 1)
        self.assertEqual([call["status"] for call in calls], ["success", "cache_hit"])
        self.assertTrue(calls[0]["billable"])
        self.assertFalse(calls[1]["billable"])

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
