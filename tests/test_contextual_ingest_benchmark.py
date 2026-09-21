import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import ingest
import rag
from evaluation import run_contextual_ingest_benchmark as benchmark


class TestContextualIngestBenchmark(unittest.TestCase):
    def test_database_identity_hashes_url_without_exposing_it(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://secret@db/test"}):
            identity = benchmark._database_identity("variant-a")

        self.assertEqual(identity["operator_label"], "variant-a")
        self.assertNotIn("secret", json.dumps(identity))

    def test_corpus_identity_is_stable_and_does_not_expose_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "cliente-secreto.md").write_text("conteudo A", encoding="utf-8")
            (root / "outro.txt").write_text("conteudo B", encoding="utf-8")

            first = benchmark._corpus_identity(root, recursive=False)
            repeated = benchmark._corpus_identity(root, recursive=False)

        serialized = json.dumps(first)
        self.assertEqual(first, repeated)
        self.assertEqual(first["file_count"], 2)
        self.assertNotIn("cliente-secreto", serialized)
        self.assertNotIn("conteudo A", serialized)

    def test_deterministic_variant_records_known_zero_contextual_usage(self):
        telemetry = benchmark._Telemetry()

        with patch.object(config, "CONTEXTUAL_RETRIEVAL_ENABLED", False), patch.object(
            ingest,
            "create_document_embeddings",
            return_value=[[0.0] * config.EMBEDDING_DIMENSIONS],
        ):
            with benchmark._capture_telemetry(telemetry):
                ingest.create_document_embeddings(["trecho"])
                result = ingest._contextualize_chunks_batch(
                    chunks_with_indices=[(0, "trecho")],
                    full_document="documento",
                    filename="fonte.md",
                )

        summary = telemetry.summary(contextualization_enabled=False)
        self.assertEqual(result, [(0, "trecho")])
        self.assertEqual(summary["contextualization"]["call_count"], 0)
        self.assertEqual(summary["contextualization"]["estimated_cost_usd"], 0.0)
        self.assertTrue(summary["contextualization"]["cost_complete"])
        self.assertEqual(summary["embeddings"]["call_count"], 1)
        self.assertFalse(summary["combined"]["cost_complete"])

    def test_llm_variant_collects_tokens_cost_and_contextualized_chunks(self):
        telemetry = benchmark._Telemetry()

        def fake_generate(*, model, model_calls, stage, **_kwargs):
            response = rag._GeneratedTextResponse(
                '["contexto curto"]',
                provider="gemini",
                model=model,
                usage={
                    "input_tokens": 100,
                    "cached_input_tokens": 10,
                    "output_tokens": 20,
                    "reasoning_tokens": 0,
                    "total_tokens": 130,
                },
            )
            rag._record_model_call(
                model_calls,
                request_id=None,
                stage=stage,
                provider="gemini",
                requested_model=model,
                response=response,
                latency_ms=5,
                status="success",
                routing_reason="test",
            )
            return response

        with patch.object(config, "CONTEXTUAL_RETRIEVAL_ENABLED", True), patch.object(
            rag, "_gemini_generate", side_effect=fake_generate
        ), patch.object(ingest, "_wait_for_contextual_slot"), benchmark._capture_telemetry(
            telemetry
        ):
            result = ingest._contextualize_chunks_batch(
                chunks_with_indices=[(0, "trecho")],
                full_document="documento",
                filename="fonte.md",
            )

        summary = telemetry.summary(contextualization_enabled=True)
        self.assertEqual(result, [(0, "contexto curto\n\ntrecho")])
        self.assertEqual(summary["contextualization"]["call_count"], 1)
        self.assertEqual(summary["contextualization"]["totals"]["total_tokens"], 130)
        self.assertEqual(summary["contextualization_batches"], 1)
        self.assertEqual(summary["contextual_chunks_applied"], 1)

    def test_reference_requires_distinct_database(self):
        current = {
            "git_commit": "abc",
            "database": {"url_sha256": "same"},
            "corpus": {"fingerprint_sha256": "corpus"},
            "configuration": {"invariants_fingerprint_sha256": "config"},
        }
        reference = json.loads(json.dumps(current))

        with self.assertRaisesRegex(ValueError, "bancos isolados diferentes"):
            benchmark._validate_reference(current, reference)

    def test_invariant_fingerprint_includes_contextual_prompt_contract(self):
        with patch.object(config, "CONTEXTUAL_RETRIEVAL_ENABLED", False):
            original = benchmark._effective_config("deterministic")
            with patch.object(
                ingest,
                "_CONTEXTUAL_INSTRUCTIONS",
                ingest._CONTEXTUAL_INSTRUCTIONS + " regra nova",
            ):
                changed = benchmark._effective_config("deterministic")

        self.assertNotEqual(
            original["invariants_fingerprint_sha256"],
            changed["invariants_fingerprint_sha256"],
        )


if __name__ == "__main__":
    unittest.main()
