import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import config
import rag


def _chunk(chunk_id: str, similarity: float) -> dict:
    return {
        "id": chunk_id,
        "document_id": f"doc-{chunk_id}",
        "filename": f"{chunk_id}.md",
        "content": f"Conteudo tecnico do documento {chunk_id}.",
        "chunk_index": 0,
        "similarity": similarity,
        "metadata": {"source_kind": "kb", "doc_priority": 5},
    }


def _gemini_response(text: str, model: str) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        model_version=model,
        candidates=[SimpleNamespace(finish_reason="STOP")],
        usage_metadata=SimpleNamespace(
            prompt_token_count=100,
            cached_content_token_count=10,
            candidates_token_count=20,
            thoughts_token_count=5,
            total_token_count=125,
        ),
    )


class TestExplicitGenerationRouting(unittest.TestCase):
    def test_invalid_model_policy_fails_early(self):
        with patch.multiple(
            config,
            GENERATION_PROVIDER="gemini",
            GENERATION_API_KEY="geracao-ficticia",
            GENERATION_MODEL="gemini-2.5-pro",
            GENERATION_MODEL_POLICY="automatic",
            EMBEDDING_PROVIDER="gemini",
            EMBEDDING_API_KEY="embedding-ficticia",
            EMBEDDING_MODEL="gemini-embedding-001",
        ):
            with self.assertRaises(EnvironmentError) as raised:
                config.validate_ai_config()

        self.assertIn("GENERATION_MODEL_POLICY", str(raised.exception))

    def test_prompt_size_does_not_change_primary_model(self):
        selected_models = []

        def generate(**kwargs):
            selected_models.append(kwargs["model"])
            return rag._GeneratedTextResponse(
                "Resposta",
                provider="openai",
                model=kwargs["model"],
            )

        with patch.multiple(
            config,
            GENERATION_PROVIDER="openai",
            GENERATION_MODEL="gpt-primary",
            GENERATION_MODEL_POLICY="primary",
            OPENAI_CONTEXTUAL_MODEL="gpt-contextual",
            ASK_MAX_TOKENS=2048,
            OPENAI_MAX_OUTPUT_TOKENS=4096,
        ), patch("rag._openai_chat_generate", side_effect=generate):
            for prompt_size in (11999, 12001):
                rag._ask_model(
                    question="Pergunta",
                    system="x" * prompt_size,
                    conversation_history=None,
                    images=None,
                )

        self.assertEqual(selected_models, ["gpt-primary", "gpt-primary"])

    def test_contextual_model_requires_explicit_policy(self):
        with patch.multiple(
            config,
            GENERATION_PROVIDER="openai",
            GENERATION_MODEL="gpt-primary",
            GENERATION_MODEL_POLICY="contextual",
            OPENAI_CONTEXTUAL_MODEL="gpt-contextual",
            ASK_MAX_TOKENS=2048,
            OPENAI_MAX_OUTPUT_TOKENS=4096,
        ), patch(
            "rag._openai_chat_generate",
            return_value=rag._GeneratedTextResponse("Resposta"),
        ) as generate_mock:
            rag._ask_model(
                question="Pergunta",
                system="Sistema",
                conversation_history=None,
                images=None,
            )

        self.assertEqual(generate_mock.call_args.kwargs["model"], "gpt-contextual")
        self.assertEqual(
            generate_mock.call_args.kwargs["routing_reason"],
            "policy:contextual",
        )


class TestProviderUsageNormalization(unittest.TestCase):
    def test_openai_usage_separates_cache_and_reasoning_without_double_counting(self):
        http_response = Mock()
        http_response.status_code = 200
        http_response.raise_for_status.return_value = None
        http_response.json.return_value = {
            "model": "gpt-5.4-2026-03-05",
            "choices": [
                {
                    "message": {"content": "Resposta"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "prompt_tokens_details": {"cached_tokens": 20},
                "completion_tokens_details": {"reasoning_tokens": 10},
            },
        }
        client = Mock()
        client.post.return_value = http_response
        model_calls = []

        with patch.multiple(
            config,
            GENERATION_BASE_URL="https://api.openai.com/v1",
            GENERATION_API_KEY="credencial-ficticia",
        ), patch("rag._get_http_client", return_value=client):
            response = rag._openai_chat_generate(
                model="gpt-5.4",
                messages=[{"role": "user", "content": "Pergunta"}],
                request_id="req-1",
                stage="generation",
                model_calls=model_calls,
            )

        self.assertEqual(
            response.usage,
            {
                "input_tokens": 80,
                "cached_input_tokens": 20,
                "output_tokens": 40,
                "reasoning_tokens": 10,
                "total_tokens": 150,
            },
        )
        self.assertEqual(model_calls[0]["model"], "gpt-5.4-2026-03-05")
        self.assertEqual(model_calls[0]["estimated_cost_usd"], 0.000955)
        self.assertEqual(model_calls[0]["status"], "success")

    def test_missing_usage_remains_unknown(self):
        http_response = Mock()
        http_response.status_code = 200
        http_response.raise_for_status.return_value = None
        http_response.json.return_value = {
            "model": "gpt-5.4",
            "choices": [{"message": {"content": "Resposta"}}],
        }
        client = Mock()
        client.post.return_value = http_response
        model_calls = []

        with patch.multiple(
            config,
            GENERATION_BASE_URL="https://api.openai.com/v1",
            GENERATION_API_KEY="credencial-ficticia",
        ), patch("rag._get_http_client", return_value=client):
            rag._openai_chat_generate(
                model="gpt-5.4",
                messages=[{"role": "user", "content": "Pergunta"}],
                request_id="req-2",
                stage="generation",
                model_calls=model_calls,
            )

        summary = rag._summarize_model_calls(model_calls)
        self.assertIsNone(model_calls[0]["usage"])
        self.assertIsNone(model_calls[0]["estimated_cost_usd"])
        self.assertEqual(summary["calls_without_usage"], 1)
        self.assertIsNone(summary["totals"]["total_tokens"])
        self.assertIsNone(summary["estimated_cost_usd"])


class TestRequestModelTrace(unittest.TestCase):
    def test_trace_separates_and_aggregates_all_generation_stages(self):
        chunks = [_chunk("a", 0.70), _chunk("b", 0.69), _chunk("c", 0.68)]
        responses = [
            _gemini_response("Pergunta reformulada", "gemini-2.5-flash"),
            _gemini_response("2,1,0", "gemini-2.5-flash"),
            _gemini_response("Resposta sem fontes", "gemini-2.5-pro"),
            _gemini_response(
                "Resposta revisada.\n\nFontes:\n- a.md",
                "gemini-2.5-pro",
            ),
        ]
        client = Mock()
        client.models.generate_content.side_effect = responses

        with patch.multiple(
            config,
            GENERATION_PROVIDER="gemini",
            GENERATION_MODEL="gemini-2.5-pro",
            GENERATION_MODEL_POLICY="primary",
            REFORMULATION_MODEL="gemini-2.5-flash",
            RERANKER_MODEL="gemini-2.5-flash",
            FULL_CONTEXT_ENABLED=False,
            RAG_ENABLE_QUERY_REFORMULATION=True,
            RAG_ENABLE_RERANKING=True,
            RERANKER_MIN_TRIGGER_SIM=0.55,
            RERANKER_MAX_TRIGGER_SIM=0.82,
            RERANKER_MAX_CANDIDATES=8,
            RAG_STRICT_ABSTAIN=True,
            RAG_MIN_RETRIEVED_CHUNKS=1,
            RAG_MIN_STRONG_SIMILARITY=0.60,
            RAG_OPERATIONAL_SIMILARITY_MARGIN=0.0,
            RAG_ENABLE_BUSINESS_RULES=False,
            RAG_ENABLE_GROUNDING_VALIDATION=True,
            RAG_REQUIRE_SOURCES_SECTION=True,
            RAG_MAX_REGEN_ATTEMPTS=1,
        ), patch(
            "rag.get_gemini",
            return_value=client,
        ), patch(
            "rag._classify_query_intent",
            return_value={"intent": "general", "modules": [], "doc_types": []},
        ), patch(
            "rag.retrieve_chunks_with_feedback",
            return_value=(chunks, [], chunks),
        ), patch(
            "rag._validate_grounded_answer",
            side_effect=[
                (False, ["Resposta vazia."], set()),
                (True, [], {"a.md"}),
            ],
        ):
            answer, _returned_chunks, trace = rag.ask(
                "E a configuracao?",
                conversation_history=[
                    {"role": "user", "content": "Como funciona o parametro?"},
                    {"role": "assistant", "content": "Ele controla o fluxo."},
                ],
            )

        self.assertIn("Resposta revisada", answer)
        self.assertEqual(
            [call["stage"] for call in trace["model_calls"]],
            ["reformulation", "rerank", "generation", "regeneration"],
        )
        self.assertEqual(
            [call["model"] for call in trace["model_calls"]],
            [
                "gemini-2.5-flash",
                "gemini-2.5-flash",
                "gemini-2.5-pro",
                "gemini-2.5-pro",
            ],
        )
        self.assertTrue(
            all(call["provider"] == "gemini" for call in trace["model_calls"])
        )
        self.assertTrue(
            all(call["finish_reason"] == "STOP" for call in trace["model_calls"])
        )
        self.assertTrue(
            all(call["request_id"] == trace["request_id"] for call in trace["model_calls"])
        )
        self.assertEqual(trace["model_usage"]["call_count"], 4)
        self.assertEqual(trace["model_usage"]["calls_without_usage"], 0)
        self.assertEqual(
            trace["model_usage"]["totals"],
            {
                "input_tokens": 360,
                "cached_input_tokens": 40,
                "output_tokens": 80,
                "reasoning_tokens": 20,
                "total_tokens": 500,
            },
        )
        self.assertTrue(trace["model_usage"]["cost_complete"])


if __name__ == "__main__":
    unittest.main()
