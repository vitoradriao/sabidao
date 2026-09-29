"""Exercita o avaliador com o pipeline real e providers inteiramente simulados."""

from contextlib import ExitStack
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import config
import evidence_gate
import jev
import rag
from evaluation import run_offline_eval as evaluator
from tests.test_jev_client import _response
from tests import test_offline_eval as offline_tests
from tests.test_semantic_grounding import policy_v2


class TestJevEvaluationStudy(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(jev.reset_shared_executor_for_tests)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.policy_path = Path(directory) / "fixture-policy.json"
        self.policy = policy_v2()
        self.policy_path.write_text(json.dumps(self.policy), encoding="utf-8")
        self.profile = json.loads((Path(evaluator.__file__).parent / "baseline_config.json").read_text(encoding="utf-8"))
        self.profile["comparison"]["unavailable_variants"] = {}
        self.profile["comparison"]["jev_study"]["grounding"]["base_variant"] = "existing"
        self.profile["comparison"]["jev_study"]["grounding"]["development_run_id"] = "synthetic-grounding-v1"
        self.profile["comparison"]["jev_study"]["grounding"]["base_selection_development_run_id"] = "synthetic-base-selection"
        self.stack.enter_context(patch.multiple(
            config, TYPESAFE_API_KEY="fake-private-key", JEV_POLICY_FILE=str(self.policy_path),
            JEV_MODEL="jev-1.13.0", JEV_MIN_REMAINING_SECONDS=0.0,
            JEV_STAGE_TIMEOUT_SECONDS=8.0, JEV_MAX_STATE_ESTIMATED_TOKENS=24000,
            JEV_GROUNDING_MAX_REGENERATIONS=1, RAG_MAX_REGEN_ATTEMPTS=1,
            JEV_RERANK_MAX_CANDIDATES=20, RAG_ENABLE_RERANKING=True,
            RAG_ENABLE_QUERY_REFORMULATION=False, RAG_STRICT_ABSTAIN=False,
            RAG_ENABLE_BUSINESS_RULES=False, RAG_ENABLE_GROUNDING_VALIDATION=False,
            JEV_EVIDENCE_GATE_ENABLED=False, JEV_SEMANTIC_GROUNDING_ENABLED=False,
            MAX_CONTEXT_CHUNKS=2, FULL_CONTEXT_ENABLED=False,
        ))
        self.stack.enter_context(patch.object(evaluator, "_database_identity", return_value=offline_tests.TestOfflineEvaluator._verified_database_identity()))
        self.chunks = [
            {"id": "private-a", "document_id": "doc-a", "filename": "private-a.md", "content": "Use S.", "similarity": .9},
            {"id": "private-b", "document_id": "doc-b", "filename": "private-b.md", "content": "Use N.", "similarity": .8},
        ]
        self.dataset = [{"id": "study-case", "question": "Qual valor usar?", "split": "development", "expected_behavior": "exact_answer", "expected_intent": "general", "expected_facts": ["Use S"], "reference_evidence": [{"source": "private-a.md", "contains": ["Use S"]}]}]
        self.stack.enter_context(patch.object(rag, "_classify_query_intent", return_value={"intent": "general", "modules": [], "doc_types": []}))
        self.stack.enter_context(patch.object(rag, "retrieve_chunks_with_feedback", side_effect=lambda *a, **kw: (copy.deepcopy(self.chunks), [], copy.deepcopy(self.chunks))))
        self.stack.enter_context(patch.object(rag, "_ask_model", side_effect=self.generate))
        self.stack.enter_context(patch.object(rag, "_gemini_generate", side_effect=self.extract))
        original_client = jev.TypeSafeClient
        self.stack.enter_context(patch.object(jev, "TypeSafeClient", side_effect=lambda: original_client(transport=self)))
        self.judgments = []
        self.transport_calls = []
        self.generation_calls = []
        self.extraction_complete = True
        self.unknown_cost = False

    def record(self, kwargs):
        stage = kwargs["stage"]
        self.generation_calls.append(stage)
        kwargs["model_calls"].append({
            "call_id": f"generation-{len(self.generation_calls)}", "stage": stage,
            "status": "success", "estimated_cost_usd": None if self.unknown_cost else .01,
            "usage": {"input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 10, "reasoning_tokens": 0, "total_tokens": 20},
        })

    def generate(self, **kwargs):
        self.record(kwargs)
        if kwargs["stage"] == "regeneration":
            return "Segundo private-a.md, use S; segundo private-b.md, use N; não há base para escolher.\n\nFontes:\n- private-a.md\n- private-b.md"
        return "Use S.\n\nFontes:\n- private-a.md"

    def extract(self, _model, **kwargs):
        self.record(kwargs)
        payload = json.loads(kwargs["contents"])
        answer = payload["answer"]
        body = answer.split("\n\nFontes:")[0]
        claims = [{"id": "private-claim", "text": body, "answer_spans": [{"start": 0, "end": len(body)}], "evidence_ids": [payload["evidence_index"][0]["id"]], "kind": "instruction"}] if self.extraction_complete else []
        return SimpleNamespace(text=json.dumps({"claims": claims}), model="fake-generator")

    def post(self, _url, *, headers, json, timeout):
        self.transport_calls.append(copy.deepcopy(json))
        questions = json["questions"]
        if "sufficiency" in questions:
            answers = {"sufficiency": {"type": "choice", "choice": "sufficient", "confidence": .95, "probabilities": {key: .9 if key == "sufficient" else .05 for key in evidence_gate.QUESTIONS["sufficiency"]["criteria"]}}}
        elif any(key.endswith(".support") for key in questions):
            support, contradiction = self.judgments.pop(0) if self.judgments else (.95, .05)
            answers = {key: {"type": "noul", "noul": support if key.endswith(".support") else contradiction} for key in questions}
        else:
            state = json["state"]
            candidates = state.get("candidatos") or [state]
            answers = {key: {"type": "noul", "noul": .9 if candidate["trecho_documental"] == "Use S." else .1} for key, candidate in zip(questions, candidates)}
        return _response({"model": "jev-1.13.0", "usage": {"input_tokens": 100, "output_tokens": 2}, "answers": answers})

    def pair(self, variants, *, audit=False):
        profile = evaluator._study_subconfig(self.profile, variants=variants, views=["ranking_ablation_same_pool" if audit else "end_to_end_same_snapshot"], pair_id="real-fixture")
        profile["comparison"]["execute_order_audit"] = audit
        return evaluator.run_paired_comparison(dataset=self.dataset, dataset_name="synthetic-study", dry_run=True, limit=None, baseline_config=profile, split="development", snapshot_id="fixture-snapshot")

    def test_real_pipeline_executes_abc_and_existing_gate(self):
        report = self.pair(["existing", "jev_rerank", "jev_rerank+evidence_gate"])
        self.assertEqual(report["status"], "complete")
        self.assertEqual(len(self.generation_calls), 3)
        self.assertTrue(any("sufficiency" in call["questions"] for call in self.transport_calls))
        gate = self.pair(["existing", "existing+evidence_gate"])
        self.assertEqual(gate["status"], "complete")
        serialized = json.dumps(gate, ensure_ascii=False)
        self.assertNotIn("fake-private-key", serialized)
        self.assertFalse("private-a.md" in serialized)
        self.assertFalse("Use S" in serialized)

    def test_real_d0_d1_support_conflict_regeneration_and_incomplete_extraction(self):
        for scores, expected, qualified in [([(.95, .05)], "supported", False), ([ (.95, .95), (.95, .05)], "supported", True), ([ (.1, .9), (.1, .9)], "rejected", False), ([ (.5, .1)], "inconclusive", False)]:
            with self.subTest(scores=scores):
                self.judgments = list(scores)
                report = self.pair(["grounding_d0", "grounding_d1"])
                result = report["summaries"]["grounding_d1"]["results"][0]
                observed = result["semantic_grounding_observation"]
                self.assertEqual(observed["status"], expected)
                self.assertEqual(observed["qualified_response"], qualified)
                self.assertEqual(observed["human_review"]["status"], "not_performed")
                if qualified:
                    self.assertEqual(observed["rounds"]["original"]["status"], "rejected")
                    self.assertEqual(observed["rounds"]["final"]["status"], "supported")
                    self.assertFalse(result["unsupported_claims"])
                    self.assertEqual(observed["regenerations"], 1)
                if expected in {"rejected", "inconclusive"}:
                    self.assertTrue(result["abstained"])
        self.extraction_complete = False
        before = len(self.transport_calls)
        report = self.pair(["grounding_d0", "grounding_d1"])
        self.assertEqual(len(self.transport_calls), before)
        self.assertEqual(report["summaries"]["grounding_d1"]["results"][0]["semantic_grounding_observation"]["status"], "inconclusive")

    def test_real_rerank_audit_reuses_pool_and_query_and_counts_calls_once(self):
        report = self.pair(["jev_rerank_pointwise", "jev_rerank_batch"], audit=True)
        audit = evaluator._order_sensitivity_observation(report)
        self.assertEqual(audit["status"], "complete")
        self.assertEqual(audit["denominator"], 2)
        self.assertEqual(audit["model_usage"]["call_count"], 9)
        self.assertEqual(report["summaries"]["jev_rerank_batch"]["model_usage"]["by_stage"]["rerank"], 1)
        for case in audit["cases"]:
            self.assertFalse(case["order_sensitive"])
            self.assertEqual(len({item["input_order_sha256"] for item in case["compositions"]}), 2)
        self.assertFalse(config.JEV_SEMANTIC_GROUNDING_ENABLED)

    def fixed_records(self):
        selection = rag._select_context(self.chunks, system="", question=self.dataset[0]["question"], conversation_history=[], images=[])
        answer = "Use S.\n\nFontes:\n- private-a.md"
        envelope = {"version": rag.CONTEXT_SELECTION_VERSION, "rendered_text": selection.rendered_text, "retained_chunks": list(selection.retained_chunks), "evidence": list(selection.evidence)}
        return [{"case_id": "study-case", "answer": answer, "answer_sha256": hashlib.sha256(answer.encode()).hexdigest(), "envelope": envelope, "envelope_sha256": evaluator._canonical_sha256(envelope)}]

    def test_fixed_response_runs_real_extraction_and_judge_without_generation(self):
        self.unknown_cost = True
        report = evaluator.run_fixed_response_judgment(records=self.fixed_records(), dataset=self.dataset, baseline_config=self.profile)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(self.generation_calls, ["claim_extraction"])
        self.assertEqual(report["model_usage"]["call_count"], 2)
        self.assertFalse(report["model_usage"]["cost_complete"])
        self.assertGreater(report["model_usage"]["known_estimated_cost_usd"], 0)
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn("Use S", serialized)
        self.assertNotIn("private-a.md", serialized)
        self.assertNotIn("private-claim", serialized)

    def test_fixed_response_rejects_changed_envelope_before_provider(self):
        records = self.fixed_records()
        records[0]["envelope"]["rendered_text"] = "Texto alterado"
        with self.assertRaisesRegex(evaluator.VariantConfigurationError, "Hash do envelope"):
            evaluator.run_fixed_response_judgment(records=records, dataset=self.dataset, baseline_config=self.profile)
        self.assertEqual(self.generation_calls, [])

    def test_complete_study_accounts_pipeline_audit_and_fixed_judgment_separately(self):
        records = self.fixed_records()
        report = evaluator.run_jev_study(dataset=self.dataset, dataset_name="synthetic-study", dry_run=True, limit=None, baseline_config=self.profile, snapshot_id="fixture-snapshot", fixed_responses=records)
        self.assertEqual(report["status"], "complete")
        self.assertEqual(report["total_model_usage"]["call_count"], 23)
        self.assertEqual(report["total_model_usage"]["by_stage"]["rerank_order_audit"], 9)
        self.assertEqual(report["fixed_response_comparison"]["model_usage"]["call_count"], 2)

    def test_human_claim_review_is_bound_to_answer_and_records_omissions(self):
        semantic = {"answer_fingerprint": "a" * 64, "claim_support": [{"claim_id": evaluator._opaque_report_id("extracted-1", prefix="claim"), "status": "supported"}]}
        review = {"status": "completed", "reviewer": "fixture-reviewer", "date": "2026-09-29", "answer_sha256": "a" * 64, "claims": [{"claim_id": "human-1", "gold_support": "supported", "extraction_found": True, "extracted_claim_id": "extracted-1"}, {"claim_id": "human-2", "gold_support": "contradicted", "extraction_found": False}]}
        result = evaluator._human_grounding_review(semantic, review)
        self.assertEqual(result["omission_count"], 1)
        self.assertEqual(result["extraction_coverage"]["denominator"], 2)
        self.assertEqual(result["confusion_matrix"], {"supported:supported": 1, "contradicted:omitted": 1})
        review["answer_sha256"] = "b" * 64
        self.assertEqual(evaluator._human_grounding_review(semantic, review)["status"], "not_performed")

    def test_infrastructure_failure_does_not_count_as_factual_improvement(self):
        trace = {"semantic_grounding": {"status": "inconclusive", "reason": "provider_error"}}
        self.assertTrue(evaluator._operational_failure(trace))
        self.assertEqual(evaluator._case_outcome({"operational_failure": True, "answerability": "no_evidence", "abstained": True}), "operational_failure")

    def test_coverage_requires_reviewed_references_and_reports_denominator(self):
        metrics, details = evaluator._retrieval_metrics(self.chunks, self.dataset[0]["reference_evidence"])
        evaluator._reviewed_coverage(metrics, details, self.dataset[0])
        self.assertIsNone(metrics["evidence_coverage_at_20"])
        self.assertEqual(details["evidence_coverage_at_40"]["unavailable_reason"], "reference_review_not_confirmed")
        metrics, details = evaluator._retrieval_metrics(self.chunks, self.dataset[0]["reference_evidence"])
        evaluator._reviewed_coverage(metrics, details, {"review": {"human_review": "approved"}})
        self.assertEqual(metrics["evidence_coverage_at_40"], 1.0)
        self.assertEqual(details["evidence_coverage_at_40"]["reference_count"], 1)

    def test_provisional_grounding_policy_requires_development_identity_before_d0(self):
        self.policy["status"] = "provisional"
        self.policy_path.write_text(json.dumps(self.policy), encoding="utf-8")
        self.pair(["grounding_d0", "grounding_d1"])
        self.profile["comparison"]["jev_study"]["grounding"]["development_run_id"] = "other-run"
        before = len(self.generation_calls)
        with self.assertRaises(EnvironmentError):
            self.pair(["grounding_d0", "grounding_d1"])
        self.assertEqual(len(self.generation_calls), before)

    def test_confirmation_rejects_historical_data_renamed_and_unregistered_pair(self):
        study = self.profile["comparison"]["jev_study"]
        study["dataset_role"] = "confirmation"
        historical = evaluator._load_dataset(Path(evaluator.__file__).parent / "datasets" / "maxpedido_eval_dataset.json")
        historical[0]["id"] = "renamed-case"
        study["confirmation"] = {"independent_dataset_id": "new-name", "dataset_sha256": evaluator._canonical_sha256(historical), "policy_status": "frozen", "policy_frozen": True, "preregistered_comparisons": ["grounding_d0_vs_grounding_d1"]}
        with self.assertRaisesRegex(evaluator.VariantConfigurationError, "histórico"):
            evaluator._validate_study_dataset(self.profile["comparison"], historical, dataset_name="new-name", variants=["grounding_d0", "grounding_d1"], split="holdout")
        study["confirmation"]["dataset_sha256"] = evaluator._canonical_sha256(self.dataset)
        with self.assertRaisesRegex(evaluator.VariantConfigurationError, "pré-registrada"):
            evaluator._validate_study_dataset(self.profile["comparison"], self.dataset, dataset_name="new-name", variants=["existing", "existing+evidence_gate"], split="holdout")

    def test_preparation_validates_fixed_inputs_without_database_or_providers(self):
        records = self.fixed_records()
        with patch.object(evaluator, "_database_identity") as database, patch.object(rag, "ask") as ask, patch.object(jev, "TypeSafeClient") as client:
            report = evaluator.prepare_jev_study(dataset=self.dataset, dataset_name="synthetic-study", baseline_config=self.profile, snapshot_id="fixture-snapshot", fixed_responses=records)
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["external_calls"], 0)
        database.assert_not_called()
        ask.assert_not_called()
        client.assert_not_called()

    def test_confirmation_provisional_declaration_is_rejected_for_legacy_ab(self):
        study = self.profile["comparison"]["jev_study"]
        study["dataset_role"] = "confirmation"
        study["confirmation"] = {"independent_dataset_id": "new-name", "preregistered_comparisons": ["existing_vs_jev_rerank"], "policy_status": "provisional", "policy_frozen": True}
        with self.assertRaisesRegex(evaluator.VariantConfigurationError, "provisional"):
            evaluator._comparison_profile(self.profile)


if __name__ == "__main__":
    unittest.main()
