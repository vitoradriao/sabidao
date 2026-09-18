import copy
import unittest
import uuid
from unittest.mock import patch

import config
import rag


def _make_kb_chunk(*, chunk_id: str, filename: str, similarity: float, content: str) -> dict:
    return {
        "id": chunk_id,
        "document_id": f"doc:{chunk_id}",
        "filename": filename,
        "content": content,
        "chunk_index": 0,
        "similarity": similarity,
        "metadata": {"source_kind": "kb", "doc_priority": 5},
    }


class TestAskIntegration(unittest.TestCase):
    def test_full_context_model_abstention_sets_insufficient_evidence_state(self):
        with patch.multiple(config, FULL_CONTEXT_ENABLED=True), patch(
            "rag._reformulate_query_with_history",
            return_value="Pergunta sem resposta",
        ), patch(
            "rag._load_full_context_docs",
            return_value="Conteudo completo da base",
        ), patch(
            "rag._ask_model",
            return_value=config.NO_ANSWER_PHRASE,
        ):
            answer, returned_chunks, trace = rag.ask("Pergunta sem resposta")

        self.assertEqual(answer, config.NO_ANSWER_PHRASE)
        self.assertEqual(returned_chunks, [])
        self.assertTrue(trace["abstained"])
        self.assertEqual(trace["abstention_reason"], "model_insufficient_evidence")
        self.assertEqual(trace["response_state"], "insufficient_evidence")

    def test_answer_generation_with_seeded_chunks(self):
        chunks = [
            _make_kb_chunk(
                chunk_id="1",
                filename="guia-maxpedido.md",
                similarity=0.89,
                content="Parametro USAGRADE habilita grade de produto no pedido.",
            )
        ]

        with patch.multiple(
            config,
            FULL_CONTEXT_ENABLED=False,
            RAG_STRICT_ABSTAIN=True,
            RAG_MIN_RETRIEVED_CHUNKS=1,
            RAG_MIN_STRONG_SIMILARITY=0.60,
            RAG_OPERATIONAL_SIMILARITY_MARGIN=0.0,
            RAG_ENABLE_GROUNDING_VALIDATION=True,
            RAG_REQUIRE_SOURCES_SECTION=True,
            RAG_MAX_REGEN_ATTEMPTS=1,
        ), patch("rag._reformulate_query_with_history", return_value="Como configurar USAGRADE?"), patch(
            "rag._classify_query_intent",
            return_value={"intent": "configuration", "modules": ["parametros_configuracao"], "doc_types": ["md"]},
        ), patch(
            "rag.retrieve_chunks_with_feedback",
            return_value=(chunks, [], chunks),
        ), patch(
            "rag._rerank_chunks_with_llm",
            side_effect=lambda _q, candidate_chunks: candidate_chunks,
        ), patch(
            "rag._ask_model",
            return_value=(
                "Ative o parametro USAGRADE na configuracao da forca de vendas "
                "[fonte: guia-maxpedido.md].\n\n"
                "Fontes:\n"
                "- guia-maxpedido.md"
            ),
        ):
            answer, returned_chunks, trace = rag.ask("Como configurar USAGRADE?")

        self.assertEqual(len(returned_chunks), 1)
        self.assertFalse(trace["abstained"])
        self.assertGreaterEqual(trace["top_similarity"], 0.89)
        self.assertIn("guia-maxpedido.md", [s.lower() for s in trace["cited_files"]])
        self.assertIn("Fontes:", answer)
        self.assertEqual(trace["response_state"], "answered")
        self.assertEqual(trace["citation_validation"]["syntax"], "valid")
        self.assertEqual(trace["citation_validation"]["semantic_support"], "not_verified")

    def test_strict_abstain_when_evidence_is_weak(self):
        weak_chunks = [
            _make_kb_chunk(
                chunk_id="2",
                filename="faq.md",
                similarity=0.30,
                content="Conteudo muito generico sem detalhe operacional.",
            )
        ]

        with patch.multiple(
            config,
            FULL_CONTEXT_ENABLED=False,
            RAG_STRICT_ABSTAIN=True,
            RAG_MIN_RETRIEVED_CHUNKS=1,
            RAG_MIN_STRONG_SIMILARITY=0.70,
            RAG_OPERATIONAL_SIMILARITY_MARGIN=0.05,
        ), patch("rag._reformulate_query_with_history", return_value="Qual campo da tabela X?"), patch(
            "rag._classify_query_intent",
            return_value={"intent": "sql_lookup", "modules": ["sql_integracao"], "doc_types": ["md"]},
        ), patch(
            "rag.retrieve_chunks_with_feedback",
            return_value=(weak_chunks, [], weak_chunks),
        ), patch("rag._rerank_chunks_with_llm", side_effect=lambda _q, candidate_chunks: candidate_chunks), patch(
            "rag._ask_model"
        ) as ask_model_mock:
            answer, _returned_chunks, trace = rag.ask("Qual campo da tabela X?")

        ask_model_mock.assert_not_called()
        self.assertTrue(trace["abstained"])
        self.assertEqual(trace["abstention_reason"], "low_similarity")
        self.assertEqual(trace["response_state"], "insufficient_evidence")
        self.assertTrue(answer.startswith(config.NO_ANSWER_PHRASE))

    def test_provider_error_has_no_citations_and_skips_regeneration(self):
        chunks = [
            _make_kb_chunk(
                chunk_id="3",
                filename="guia-maxpedido.md",
                similarity=0.89,
                content="Parametro USAGRADE habilita grade de produto no pedido.",
            )
        ]
        provider_error = rag._provider_error_response(
            "O servico esta sobrecarregado no momento. Tente novamente em alguns segundos."
        )

        with patch.multiple(
            config,
            FULL_CONTEXT_ENABLED=False,
            RAG_STRICT_ABSTAIN=True,
            RAG_MIN_RETRIEVED_CHUNKS=1,
            RAG_MIN_STRONG_SIMILARITY=0.60,
            RAG_OPERATIONAL_SIMILARITY_MARGIN=0.0,
            RAG_ENABLE_GROUNDING_VALIDATION=True,
            RAG_REQUIRE_SOURCES_SECTION=True,
            RAG_MAX_REGEN_ATTEMPTS=1,
        ), patch("rag._reformulate_query_with_history", return_value="Como configurar USAGRADE?"), patch(
            "rag._classify_query_intent",
            return_value={"intent": "configuration", "modules": ["parametros_configuracao"], "doc_types": ["md"]},
        ), patch(
            "rag.retrieve_chunks_with_feedback",
            return_value=(chunks, [], chunks),
        ), patch(
            "rag._rerank_chunks_with_llm",
            side_effect=lambda _q, candidate_chunks: candidate_chunks,
        ), patch("rag._ask_model", return_value=provider_error) as ask_model_mock:
            answer, _returned_chunks, trace = rag.ask("Como configurar USAGRADE?")

        self.assertEqual(answer, str(provider_error))
        self.assertEqual(ask_model_mock.call_count, 1)
        self.assertEqual(trace["response_state"], "provider_error")
        self.assertEqual(trace["cited_files"], [])
        self.assertEqual(trace["regeneration_attempts"], 0)
        self.assertEqual(trace["citation_validation"]["syntax"], "not_applicable")
        self.assertEqual(trace["citation_validation"]["semantic_support"], "not_evaluated")


class TestCorrectionWorkflowIntegration(unittest.TestCase):
    def test_submit_approve_publish_and_retrieve_feedback_chunk(self):
        state = {
            "feedback_items": {},
            "feedback_chunks": {},
        }

        def fake_db_call(function_name, params, expect_rows=True):
            if function_name == "submit_feedback":
                feedback_id = str(uuid.uuid4())
                state["feedback_items"][feedback_id] = {
                    "id": feedback_id,
                    "query": params["p_query"],
                    "bot_answer": params.get("p_bot_answer"),
                    "corrected_answer": params["p_corrected_answer"],
                    "scope": copy.deepcopy(params.get("p_scope") or {"level": "global"}),
                    "status": "PENDING",
                    "tags": copy.deepcopy(params.get("p_tags") or []),
                }
                return feedback_id

            if function_name == "approve_feedback":
                feedback_id = str(params["p_id"])
                state["feedback_items"][feedback_id]["status"] = "APPROVED"
                return []

            if function_name == "publish_feedback":
                feedback_id = str(params["p_id"])
                item = state["feedback_items"][feedback_id]
                item["status"] = "PUBLISHED"
                chunk_id = str(uuid.uuid4())
                scope = params.get("p_scope_override") or item.get("scope") or {"level": "global"}
                state["feedback_chunks"][chunk_id] = {
                    "id": chunk_id,
                    "feedback_item_id": feedback_id,
                    "content": params["p_chunk_text"],
                    "scope": copy.deepcopy(scope),
                    "active": True,
                }
                return chunk_id

            if function_name == "search_feedback_chunks":
                rows = []
                for chunk in state["feedback_chunks"].values():
                    if not chunk.get("active"):
                        continue
                    item = state["feedback_items"][chunk["feedback_item_id"]]
                    if item.get("status") != "PUBLISHED":
                        continue
                    scope = chunk.get("scope") or {}
                    if params.get("scope_level") and scope.get("level", "global") != params["scope_level"]:
                        continue
                    if params.get("scope_tenant") and scope.get("tenant", "") != params["scope_tenant"]:
                        continue
                    if params.get("scope_erp") and scope.get("erp", "") != params["scope_erp"]:
                        continue
                    if params.get("scope_version") and scope.get("version", "") != params["scope_version"]:
                        continue
                    rows.append(
                        {
                            "id": chunk["id"],
                            "feedback_item_id": chunk["feedback_item_id"],
                            "content": chunk["content"],
                            "scope": copy.deepcopy(scope),
                            "similarity": 0.93,
                        }
                    )
                return rows[: int(params.get("match_count") or 6)]

            raise AssertionError(f"Unexpected RPC call: {function_name}")

        def fake_db_select(table, select="*", filters=None):
            if table == "feedback_items":
                filters = filters or {}
                raw_id = str(filters.get("id", ""))
                if raw_id.startswith("eq."):
                    feedback_id = raw_id[3:]
                    item = state["feedback_items"].get(feedback_id)
                    return [copy.deepcopy(item)] if item else []
            return []

        with patch("rag.db_call_rows", side_effect=fake_db_call), patch(
            "rag.db_select_rows", side_effect=fake_db_select
        ), patch("rag.create_document_embedding", return_value=[0.01] * 1536), patch(
            "rag._get_cached_query_embedding", return_value=[0.01] * 1536
        ):
            feedback_id = rag.submit_feedback_item(
                query="Qual o caminho do parametro USAGRADE?",
                bot_answer="Resposta parcial",
                corrected_answer="Abrir menu X e parametro Y.",
                tags=["maxpedido", "parametro"],
                scope={"level": "tenant", "tenant": "ACME", "erp": "Winthor", "version": "12.1"},
                created_by="discord:qa",
                platform="discord",
            )

            rag.approve_feedback_item(feedback_id, reviewer="discord:reviewer", note="ok")
            chunk_id = rag.publish_feedback_item(feedback_id, publisher="discord:publisher")

            retrieved = rag._search_feedback_memory_chunks(
                "USAGRADE",
                scope={"level": "tenant", "tenant": "ACME", "erp": "Winthor", "version": "12.1"},
                scope_level="tenant",
                max_results=3,
                threshold=0.1,
            )

        self.assertTrue(feedback_id)
        self.assertTrue(chunk_id)
        self.assertEqual(len(retrieved), 1)
        self.assertEqual(retrieved[0]["metadata"]["source_kind"], "feedback_scoped")
        self.assertEqual(retrieved[0]["similarity"], 0.93)
        self.assertEqual(retrieved[0]["vector_similarity"], 0.93)
        self.assertEqual(retrieved[0]["feedback_priority"], 2)
        self.assertIsNone(retrieved[0]["fusion_score"])
        self.assertIn("Pergunta original", retrieved[0]["content"])


if __name__ == "__main__":
    unittest.main()
