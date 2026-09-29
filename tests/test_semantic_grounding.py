"""Fixtures sintéticas do grounding ativo; não representam calibração operacional."""

import hashlib
from html import escape
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import config
import evidence_gate
import grounding
import jev
import rag


def policy_v2(*, low=0.2, high=0.8, gate=True):
    policy = {
        "schema_version": 2,
        "model": "jev-1.13.0",
        "development_run_id": "synthetic-grounding-v1",
        "status": "frozen",
        "grounding": {
            "prompt_version": grounding.JUDGMENT_PROMPT_VERSION,
            "thresholds": {
                "support": {"low": low, "high": high},
                "contradiction": {"low": low, "high": high},
            },
        },
    }
    if gate:
        policy["gate"] = {
            "prompt_version": evidence_gate.PROMPT_VERSION,
            "thresholds": {
                label: {"min_confidence": 0.8, "min_probability_margin": 0.6}
                for label in evidence_gate.NEGATIVE_DECISIONS
            },
        }
    return policy


def selection(*items):
    chunks = []
    evidence = []
    for evidence_id, content, source in items:
        chunks.append({"content": content, "filename": source})
        evidence.append({
            "evidence_id": evidence_id,
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "spans": [{"start": 0, "end": len(content), "source_length": len(content)}],
            "source": source,
            "location": {"filename": source, "chunk_index": 0},
        })
    return SimpleNamespace(
        retained_chunks=tuple(chunks),
        evidence=tuple(evidence),
        rendered_text="\n".join(escape(content, quote=False) for _, content, _ in items),
    )


def claim_set(answer, selected, claims):
    index, fingerprint = grounding.build_evidence_index(
        selected.evidence, selected.retained_chunks, selected.rendered_text,
    )
    raw_claims = []
    for position, (text, evidence_ids, kind) in enumerate(claims, start=1):
        start = answer.index(text)
        raw_claims.append({
            "id": f"claim-{position}",
            "text": text,
            "answer_spans": [{"start": start, "end": start + len(text)}],
            "evidence_ids": evidence_ids,
            "kind": kind,
        })
    return grounding.parse_claim_set(
        answer=answer,
        raw_json=json.dumps({"claims": raw_claims}, ensure_ascii=False),
        evidence_index=index,
        evidence_fingerprint=fingerprint,
        extractor_model="fixture-generator",
        max_claims=12,
    )


class FakeClient:
    def __init__(self, probabilities):
        self.probabilities = list(probabilities)
        self.calls = []
        self.closed = False

    def decide(self, state, questions, **kwargs):
        self.calls.append((state, questions, kwargs))
        support, contradiction = self.probabilities.pop(0)
        answers = {}
        for question_id in questions:
            value = support if question_id.endswith(".support") else contradiction
            answers[question_id] = {"type": "noul", "noul": value}
        return jev.DecisionResult(
            status="ok",
            model_requested="jev-1.13.0",
            model_effective="jev-1.13.0",
            answers=answers,
            usage={"input_tokens": 100, "output_tokens": 4},
            latency_ms=2,
            error_code=None,
            request_id=kwargs["request_id"],
            call_id=f"call-{len(self.calls)}",
            estimated_cost_usd=0.0000042,
            cost_status="estimated",
        )

    def close(self):
        self.closed = True


class TestSemanticGrounding(unittest.TestCase):
    def setUp(self):
        self.settings = patch.multiple(
            config,
            JEV_MODEL="jev-1.13.0",
            JEV_MAX_STATE_ESTIMATED_TOKENS=24000,
            JEV_STAGE_TIMEOUT_SECONDS=8.0,
            JEV_MIN_REMAINING_SECONDS=0.0,
            JEV_GROUNDING_MAX_REGENERATIONS=1,
            RAG_MAX_REGEN_ATTEMPTS=1,
            RAG_ENABLE_GROUNDING_VALIDATION=False,
        )
        self.settings.start()
        self.addCleanup(self.settings.stop)

    def test_dual_noul_classification_has_no_majority_or_complement_assumption(self):
        policy = grounding.load_grounding_policy(policy_v2())
        cases = {
            (0.9, 0.1): "supported",
            (0.1, 0.1): "unsupported",
            (0.1, 0.9): "contradicted",
            (0.9, 0.9): "conflicting_evidence",
            (0.5, 0.1): "inconclusive",
        }
        for probabilities, expected in cases.items():
            with self.subTest(probabilities=probabilities):
                self.assertEqual(
                    grounding.classify_claim(*probabilities, policy), expected,
                )
        self.assertEqual(
            grounding.aggregate_status([
                {"status": "supported"}, {"status": "conflicting_evidence"},
                {"status": "supported"},
            ]),
            "rejected",
        )

    def test_policy_v2_validates_enabled_sections_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps(policy_v2(gate=False)), encoding="utf-8")
            with patch.multiple(
                config,
                JEV_POLICY_FILE=str(path), TYPESAFE_API_KEY="fixture",
                FULL_CONTEXT_ENABLED=False,
            ):
                raw, section = config.validate_semantic_grounding_config()
                self.assertEqual(raw["schema_version"], 2)
                self.assertEqual(section["thresholds"]["support"]["high"], 0.8)
                with self.assertRaisesRegex(EnvironmentError, "seção gate"):
                    config.validate_evidence_gate_config()
            complete = policy_v2()
            path.write_text(json.dumps(complete), encoding="utf-8")
            gate = evidence_gate.load_policy(str(path), model="jev-1.13.0")
            self.assertEqual(
                evidence_gate.empty_result("fixture", policy=gate)["policy_version"],
                evidence_gate.fingerprint(complete),
            )
            broken = policy_v2(low=0.8, high=0.2)
            with self.assertRaisesRegex(EnvironmentError, "0 <= low < high <= 1"):
                grounding.load_grounding_policy(broken)

    def test_round_uses_complete_envelope_and_records_per_call_trace(self):
        answer = "Defina PIX como S. A rotina 530 aplica a mudança."
        selected = selection(
            ("ev-1", "Defina PIX como S.", "a.md"),
            ("ev-2", "A rotina 530 aplica a mudança.", "b.md"),
        )
        claims = claim_set(answer, selected, [
            ("Defina PIX como S.", ["ev-1"], "instruction"),
            ("A rotina 530 aplica a mudança.", ["ev-2"], "factual"),
        ])
        client = FakeClient([(0.95, 0.05)])
        calls = []
        with patch("rag._extract_answer_claims", return_value=claims), patch(
            "rag.jev.TypeSafeClient", return_value=client,
        ):
            result = rag._semantic_grounding_round(
                question="Como configurar?", answer=answer, selection=selected,
                policy=policy_v2(),
                grounding_policy=grounding.load_grounding_policy(policy_v2()),
                request_id="request-1", model_calls=calls,
            )
        self.assertEqual(result["status"], "supported")
        self.assertEqual(result["counts"]["supported"], 2)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(
            {item["id"] for item in client.calls[0][0]["evidence_index"]},
            {"ev-1", "ev-2"},
        )
        self.assertEqual(client.calls[0][0]["evidence_context"], selected.rendered_text)
        self.assertEqual(len(client.calls[0][1]), 4)
        self.assertEqual(calls[0]["stage"], "semantic_grounding")
        self.assertIn("state_sha256", calls[0])
        serialized = json.dumps(grounding.result_trace(result), ensure_ascii=False)
        self.assertNotIn(answer, serialized)
        self.assertNotIn("Defina PIX", serialized)

    def test_incomplete_extraction_and_state_limit_abstain_without_jev_call(self):
        answer = "Defina PIX como S."
        selected = selection(("ev-1", answer, "a.md"))
        incomplete = grounding.empty_claim_set(
            answer, "fingerprint", "fixture", "incomplete", "uncovered_answer_spans",
        )
        client = FakeClient([])
        with patch("rag._extract_answer_claims", return_value=incomplete), patch(
            "rag.jev.TypeSafeClient", return_value=client,
        ):
            result = rag._semantic_grounding_round(
                question="Como?", answer=answer, selection=selected,
                policy=policy_v2(),
                grounding_policy=grounding.load_grounding_policy(policy_v2()),
                request_id="request-1", model_calls=[],
            )
        self.assertEqual(result["status"], "inconclusive")
        self.assertEqual(client.calls, [])

        complete = claim_set(answer, selected, [(answer, ["ev-1"], "instruction")])
        with patch.object(config, "JEV_MAX_STATE_ESTIMATED_TOKENS", 1), patch(
            "rag._extract_answer_claims", return_value=complete,
        ), patch("rag.jev.TypeSafeClient", return_value=client):
            result = rag._semantic_grounding_round(
                question="Como?", answer=answer, selection=selected,
                policy=policy_v2(),
                grounding_policy=grounding.load_grounding_policy(policy_v2()),
                request_id="request-1", model_calls=[],
            )
        self.assertEqual(result["reason"], "state_limit")
        self.assertEqual(client.calls, [])

    def test_claim_batches_repeat_complete_evidence_without_combining_probabilities(self):
        answer = "Defina PIX como S. A rotina 530 aplica a mudança."
        selected = selection(
            ("ev-1", "Defina PIX como S.", "a.md"),
            ("ev-2", "A rotina 530 aplica a mudança.", "b.md"),
        )
        claims = claim_set(answer, selected, [
            ("Defina PIX como S.", ["ev-1"], "instruction"),
            ("A rotina 530 aplica a mudança.", ["ev-2"], "factual"),
        ])
        client = FakeClient([(0.95, 0.05), (0.95, 0.05)])

        def estimate(text, **_kwargs):
            return (100 if text.count('"id": "claim-') <= 1 else 300, "fixture")

        with patch("rag._extract_answer_claims", return_value=claims), patch(
            "rag.jev.TypeSafeClient", return_value=client,
        ), patch("rag._count_context_text", side_effect=estimate), patch.object(
            config, "JEV_MAX_STATE_ESTIMATED_TOKENS", 200,
        ):
            result = rag._semantic_grounding_round(
                question="Como configurar?", answer=answer, selection=selected,
                policy=policy_v2(),
                grounding_policy=grounding.load_grounding_policy(policy_v2()),
                request_id="request-1", model_calls=[],
            )
        self.assertEqual(result["status"], "supported")
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(all(
            call[0]["evidence_context"] == selected.rendered_text for call in client.calls
        ))
        self.assertEqual(
            [item["call_id"] for item in result["claim_support"]],
            ["call-1", "call-2"],
        )

    def test_one_shared_regeneration_is_fully_revalidated(self):
        selected = selection(("ev-1", "Use S conforme a.md.", "a.md"))
        initial = grounding.empty_grounding_result("fixture")
        initial.update(
            status="rejected",
            claim_support=[{
                "claim_id": "claim-1", "status": "unsupported",
            }],
            _claims=[{"id": "claim-1", "text": "Use N."}],
        )
        final = grounding.empty_grounding_result("fixture")
        final.update(status="supported")
        revised = "Use S conforme a.md.\n\nFontes:\n- a.md"
        with patch("rag._semantic_grounding_round", side_effect=[initial, final]) as judge, patch(
            "rag._ask_model", return_value=revised,
        ) as regenerate:
            answer, errors, cited, attempts, result = rag._apply_semantic_grounding(
                answer="Use N.\n\nFontes:\n- a.md", question="Qual valor?", system="fixture",
                selection=selected, allowed_sources={"a.md"}, source_display_map=None,
                request_id="request-1", model_calls=[], policy=policy_v2(),
                grounding_policy=grounding.load_grounding_policy(policy_v2()),
                regeneration_attempts=0,
            )
        self.assertEqual(answer, revised)
        self.assertEqual(errors, [])
        self.assertEqual(cited, {"a.md"})
        self.assertEqual(attempts, 1)
        self.assertEqual(result["status"], "supported")
        self.assertEqual(judge.call_count, 2)
        self.assertEqual(regenerate.call_count, 1)
        self.assertEqual([item["status"] for item in result["rounds"]], ["rejected", "supported"])

    def test_failure_after_regeneration_never_releases_rejected_answer(self):
        selected = selection(("ev-1", "Use S.", "a.md"))
        rejected = grounding.empty_grounding_result("fixture")
        rejected.update(
            status="rejected",
            claim_support=[{"claim_id": "claim-1", "status": "contradicted"}],
            _claims=[{"id": "claim-1", "text": "Use N."}],
        )
        inconclusive = grounding.empty_grounding_result("fixture")
        inconclusive.update(status="inconclusive", reason="provider_error")
        with patch("rag._semantic_grounding_round", side_effect=[rejected, inconclusive]), patch(
            "rag._ask_model", return_value="Use N.\n\nFontes:\n- a.md",
        ):
            answer, errors, cited, attempts, result = rag._apply_semantic_grounding(
                answer="Use N.\n\nFontes:\n- a.md", question="Qual valor?", system="fixture",
                selection=selected, allowed_sources={"a.md"}, source_display_map=None,
                request_id="request-1", model_calls=[], policy=policy_v2(),
                grounding_policy=grounding.load_grounding_policy(policy_v2()),
                regeneration_attempts=0,
            )
        self.assertTrue(answer.startswith(config.NO_ANSWER_PHRASE))
        self.assertEqual(errors, ["semantic_grounding_inconclusive"])
        self.assertEqual(cited, set())
        self.assertEqual(attempts, 1)
        self.assertEqual(result["status"], "inconclusive")


if __name__ == "__main__":
    unittest.main()
