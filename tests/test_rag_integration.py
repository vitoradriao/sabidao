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
            side_effect=lambda _q, candidate_chunks, **_kwargs: candidate_chunks,
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
        ), patch("rag._rerank_chunks_with_llm", side_effect=lambda _q, candidate_chunks, **_kwargs: candidate_chunks), patch(
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
            side_effect=lambda _q, candidate_chunks, **_kwargs: candidate_chunks,
        ), patch("rag._ask_model", return_value=provider_error) as ask_model_mock:
            answer, _returned_chunks, trace = rag.ask("Como configurar USAGRADE?")

        self.assertEqual(answer, str(provider_error))
        self.assertEqual(ask_model_mock.call_count, 1)
        self.assertEqual(trace["response_state"], "provider_error")
        self.assertEqual(trace["cited_files"], [])
        self.assertEqual(trace["regeneration_attempts"], 0)
        self.assertEqual(trace["citation_validation"]["syntax"], "not_applicable")
        self.assertEqual(trace["citation_validation"]["semantic_support"], "not_evaluated")


class TestModuleGlobalChallengerIntegration(unittest.TestCase):
    def setUp(self):
        self.filtered_maxpag = _make_kb_chunk(
            chunk_id="maxpag",
            filename="12-MAXPAG.md",
            similarity=0.774,
            content="Configuracoes do MaxPag.",
        )
        self.canonical_account = _make_kb_chunk(
            chunk_id="account",
            filename="04-PARAMETROS-E-CONFIGURACAO.md",
            similarity=0.844,
            content=(
                "CON_USACREDRCA e EXIBIR_SALDOCC_DISPONIVEL configuram a conta corrente. "
                "MXSUSUARI.USADEBCREDRCA e CON_TIPOMOVCCRCA complementam o fluxo."
            ),
        )

    def _search_by_plan(self, _query, **kwargs):
        modules = (kwargs.get("query_plan") or {}).get("modules") or []
        if modules:
            return [copy.deepcopy(self.filtered_maxpag)]
        return [copy.deepcopy(self.canonical_account)]

    def test_wrong_module_keeps_canonical_document_reachable_with_bounded_cost(self):
        retrieval_trace = {}
        wrong_plan = {
            "intent": "general",
            "modules": ["financeiro_pagamentos"],
            "doc_types": ["md"],
        }

        with patch.multiple(
            config,
            RAG_ENABLE_GLOBAL_CHALLENGER=True,
            RAG_GLOBAL_CHALLENGER_COUNT=4,
            RAG_GLOBAL_CHALLENGER_FETCH_LIMIT=16,
            RAG_FILTER_BY_MODULE=True,
            RAG_FILTER_BY_DOC_TYPE=True,
            SECTION_MATCH_COUNT=12,
            CHUNK_FETCH_LIMIT=80,
            DB_STATEMENT_TIMEOUT_MS=15000,
        ), patch(
            "rag._search_feedback_memory_chunks",
            return_value=[],
        ), patch(
            "rag.search_relevant_sections",
            return_value=[],
        ), patch(
            "rag.search_similar_chunks",
            side_effect=self._search_by_plan,
        ) as search_mock:
            merged, _feedback, kb_chunks = rag.retrieve_chunks_with_feedback(
                "parametros relacionados a conta corrente do maxpedido",
                query_plan=wrong_plan,
                scope=None,
                retrieval_trace=retrieval_trace,
            )

        self.assertIn(
            "04-PARAMETROS-E-CONFIGURACAO.md",
            [chunk["filename"] for chunk in kb_chunks],
        )
        self.assertEqual(len(search_mock.call_args_list), 2)
        challenger_call = search_mock.call_args_list[1]
        self.assertEqual(challenger_call.kwargs["query_plan"]["modules"], [])
        self.assertEqual(challenger_call.kwargs["max_results"], 4)
        self.assertEqual(challenger_call.kwargs["candidate_limit"], 16)
        self.assertEqual(retrieval_trace["additional_database_searches"], 1)
        self.assertEqual(retrieval_trace["additional_embedding_calls"], 0)
        self.assertEqual(retrieval_trace["additional_generation_calls"], 0)
        self.assertGreaterEqual(retrieval_trace["global_challenger_latency_ms"], 0)
        self.assertEqual(retrieval_trace["database_statement_timeout_ms"], 15000)
        self.assertEqual(len(merged), 2)

    def test_correct_module_preserves_filtered_copy_when_challenger_duplicates_it(self):
        supporting_chunk = _make_kb_chunk(
            chunk_id="support",
            filename="04-PARAMETROS-E-CONFIGURACAO.md",
            similarity=0.821,
            content="Detalhes complementares de conta corrente.",
        )
        global_related = _make_kb_chunk(
            chunk_id="related",
            filename="08-CONTA-CORRENTE.md",
            similarity=0.814,
            content="Regras relacionadas a conta corrente.",
        )

        def search(_query, **kwargs):
            modules = (kwargs.get("query_plan") or {}).get("modules") or []
            if modules:
                return [copy.deepcopy(self.canonical_account), supporting_chunk]
            return [copy.deepcopy(self.canonical_account), global_related]

        with patch.multiple(
            config,
            RAG_ENABLE_GLOBAL_CHALLENGER=True,
            RAG_GLOBAL_CHALLENGER_COUNT=4,
            RAG_GLOBAL_CHALLENGER_FETCH_LIMIT=16,
            RAG_FILTER_BY_MODULE=True,
            RAG_FILTER_BY_DOC_TYPE=True,
        ), patch(
            "rag._search_feedback_memory_chunks",
            return_value=[],
        ), patch(
            "rag.search_relevant_sections",
            return_value=[],
        ), patch(
            "rag.search_similar_chunks",
            side_effect=search,
        ):
            _merged, _feedback, kb_chunks = rag.retrieve_chunks_with_feedback(
                "configurar conta corrente",
                query_plan={
                    "intent": "configuration",
                    "modules": ["parametros_configuracao"],
                    "doc_types": ["md"],
                },
                scope=None,
            )

        canonical_chunks = [chunk for chunk in kb_chunks if chunk["id"] == "account"]
        self.assertEqual(len(canonical_chunks), 1)
        self.assertEqual(canonical_chunks[0]["routing_scope"], "filtered")
        self.assertIn("global_challenger", rag._routing_scope_counts(kb_chunks))

    def test_challenger_failure_keeps_filtered_results_available(self):
        retrieval_trace = {}

        with patch.multiple(
            config,
            RAG_ENABLE_GLOBAL_CHALLENGER=True,
            RAG_FILTER_BY_MODULE=True,
            RAG_FILTER_BY_DOC_TYPE=True,
        ), patch(
            "rag._search_feedback_memory_chunks",
            return_value=[],
        ), patch(
            "rag.search_relevant_sections",
            return_value=[],
        ), patch(
            "rag.search_similar_chunks",
            side_effect=[copy.deepcopy([self.filtered_maxpag]), RuntimeError("db unavailable")],
        ):
            merged, _feedback, kb_chunks = rag.retrieve_chunks_with_feedback(
                "configurar conta corrente",
                query_plan={
                    "intent": "configuration",
                    "modules": ["financeiro_pagamentos"],
                    "doc_types": ["md"],
                },
                scope=None,
                retrieval_trace=retrieval_trace,
            )

        self.assertEqual([chunk["id"] for chunk in kb_chunks], ["maxpag"])
        self.assertEqual([chunk["id"] for chunk in merged], ["maxpag"])
        self.assertTrue(retrieval_trace["relaxation_attempted"])
        self.assertFalse(retrieval_trace["relaxation_applied"])
        self.assertEqual(retrieval_trace["global_challenger_status"], "failed")
        self.assertEqual(retrieval_trace["global_challenger_error"], "RuntimeError")

    def test_wrong_module_answer_uses_canonical_challenger_and_traces_selection(self):
        filtered_neighbor = _make_kb_chunk(
            chunk_id="maxpag-neighbor",
            filename="12-MAXPAG.md",
            similarity=0.761,
            content="Trecho adjacente sobre as configuracoes do MaxPag.",
        )
        filtered_neighbor["document_id"] = self.filtered_maxpag["document_id"]
        filtered_neighbor["chunk_index"] = 1
        filtered_neighbor["retrieval_origin"] = "neighbor"
        filtered_neighbor["is_neighbor"] = True
        filtered_neighbor["seed_chunk_id"] = self.filtered_maxpag["id"]

        canonical_neighbor = _make_kb_chunk(
            chunk_id="account-neighbor",
            filename="04-PARAMETROS-E-CONFIGURACAO.md",
            similarity=0.812,
            content="Trecho adjacente com detalhes do fluxo de conta corrente.",
        )
        canonical_neighbor["document_id"] = self.canonical_account["document_id"]
        canonical_neighbor["chunk_index"] = 1
        canonical_neighbor["retrieval_origin"] = "neighbor"
        canonical_neighbor["is_neighbor"] = True
        canonical_neighbor["seed_chunk_id"] = self.canonical_account["id"]

        def search(_query, **kwargs):
            modules = (kwargs.get("query_plan") or {}).get("modules") or []
            if modules:
                return [copy.deepcopy(self.filtered_maxpag), filtered_neighbor]
            return [copy.deepcopy(self.canonical_account), canonical_neighbor]

        model_answer = (
            "A configuracao usa CON_USACREDRCA, EXIBIR_SALDOCC_DISPONIVEL, "
            "MXSUSUARI.USADEBCREDRCA e CON_TIPOMOVCCRCA.\n\n"
            "Fontes:\n"
            "- 04-PARAMETROS-E-CONFIGURACAO.md"
        )

        with patch.multiple(
            config,
            FULL_CONTEXT_ENABLED=False,
            RAG_ENABLE_GLOBAL_CHALLENGER=True,
            RAG_GLOBAL_CHALLENGER_COUNT=4,
            RAG_GLOBAL_CHALLENGER_FETCH_LIMIT=16,
            RAG_FILTER_BY_MODULE=True,
            RAG_FILTER_BY_DOC_TYPE=True,
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
            RAG_MAX_REGEN_ATTEMPTS=0,
        ), patch(
            "rag._reformulate_query_with_history",
            return_value="parametros relacionados a conta corrente do maxpedido",
        ), patch(
            "rag._classify_query_intent",
            return_value={
                "intent": "general",
                "modules": ["financeiro_pagamentos"],
                "doc_types": ["md"],
            },
        ), patch(
            "rag._search_feedback_memory_chunks",
            return_value=[],
        ), patch(
            "rag.search_relevant_sections",
            return_value=[],
        ), patch(
            "rag.search_similar_chunks",
            side_effect=search,
        ), patch(
            "rag._gemini_generate",
            return_value=rag._GeneratedTextResponse("2,3,0,1"),
        ) as reranker_mock, patch(
            "rag._ask_model",
            return_value=model_answer,
        ) as ask_model_mock:
            answer, returned_chunks, trace = rag.ask(
                "Quais os parametros de conta corrente do maxPedido?"
            )

        self.assertTrue(
            all(
                fact in answer
                for fact in (
                    "CON_USACREDRCA",
                    "EXIBIR_SALDOCC_DISPONIVEL",
                    "MXSUSUARI.USADEBCREDRCA",
                    "CON_TIPOMOVCCRCA",
                )
            )
        )
        self.assertIn(
            "04-PARAMETROS-E-CONFIGURACAO.md",
            [chunk["filename"] for chunk in returned_chunks],
        )
        self.assertEqual(returned_chunks[0]["id"], "account")
        returned_neighbor = next(
            chunk for chunk in returned_chunks if chunk["id"] == "account-neighbor"
        )
        self.assertTrue(returned_neighbor["is_neighbor"])
        self.assertEqual(returned_neighbor["seed_chunk_id"], "account")
        self.assertNotIn("12-MAXPAG.md", trace["cited_files"])
        self.assertEqual(trace["retrieval_scope"]["filtered_candidate_count"], 2)
        self.assertEqual(
            trace["retrieval_scope"]["global_challenger_candidate_count"],
            2,
        )
        self.assertEqual(
            trace["retrieval_scope"]["selected_candidate_counts"],
            {"global_challenger": 2, "filtered": 2},
        )
        self.assertEqual(reranker_mock.call_count, 1)
        system = ask_model_mock.call_args.kwargs["system"]
        self.assertIn("<module_relaxation_policy>", system)
        self.assertIn("nao existe", system)
        self.assertEqual(ask_model_mock.call_count, 1)

    def test_ambiguous_query_abstains_only_after_global_challenger(self):
        def search(_query, **kwargs):
            modules = (kwargs.get("query_plan") or {}).get("modules") or []
            return [copy.deepcopy(self.filtered_maxpag)] if modules else []

        with patch.multiple(
            config,
            FULL_CONTEXT_ENABLED=False,
            RAG_ENABLE_GLOBAL_CHALLENGER=True,
            RAG_GLOBAL_CHALLENGER_COUNT=4,
            RAG_GLOBAL_CHALLENGER_FETCH_LIMIT=16,
            RAG_FILTER_BY_MODULE=True,
            RAG_FILTER_BY_DOC_TYPE=True,
            RAG_ENABLE_RERANKING=False,
            RAG_STRICT_ABSTAIN=True,
            RAG_MIN_RETRIEVED_CHUNKS=1,
            RAG_MIN_STRONG_SIMILARITY=0.60,
            RAG_OPERATIONAL_SIMILARITY_MARGIN=0.0,
            RAG_ENABLE_BUSINESS_RULES=False,
            RAG_ENABLE_GROUNDING_VALIDATION=True,
            RAG_REQUIRE_SOURCES_SECTION=True,
            RAG_MAX_REGEN_ATTEMPTS=0,
        ), patch(
            "rag._reformulate_query_with_history",
            return_value="a configuracao desconhecida existe?",
        ), patch(
            "rag._classify_query_intent",
            return_value={
                "intent": "configuration",
                "modules": ["financeiro_pagamentos"],
                "doc_types": ["md"],
            },
        ), patch(
            "rag._search_feedback_memory_chunks",
            return_value=[],
        ), patch(
            "rag.search_relevant_sections",
            return_value=[],
        ), patch(
            "rag.search_similar_chunks",
            side_effect=search,
        ) as search_mock, patch(
            "rag._ask_model",
            return_value=config.NO_ANSWER_PHRASE,
        ) as ask_model_mock:
            answer, _returned_chunks, trace = rag.ask(
                "A configuracao desconhecida existe?"
            )

        self.assertEqual(answer, config.NO_ANSWER_PHRASE)
        self.assertTrue(trace["abstained"])
        self.assertEqual(trace["abstention_reason"], "model_insufficient_evidence")
        self.assertTrue(trace["retrieval_scope"]["relaxation_applied"])
        self.assertEqual(len(search_mock.call_args_list), 2)
        self.assertEqual(ask_model_mock.call_count, 1)

    def test_strict_abstain_does_not_exceed_additional_search_budget(self):
        weak_filtered = copy.deepcopy(self.filtered_maxpag)
        weak_filtered["similarity"] = 0.40

        def search(_query, **kwargs):
            modules = (kwargs.get("query_plan") or {}).get("modules") or []
            return [copy.deepcopy(weak_filtered)] if modules else []

        with patch.multiple(
            config,
            FULL_CONTEXT_ENABLED=False,
            RAG_ENABLE_GLOBAL_CHALLENGER=True,
            RAG_GLOBAL_CHALLENGER_COUNT=4,
            RAG_GLOBAL_CHALLENGER_FETCH_LIMIT=16,
            RAG_FILTER_BY_MODULE=True,
            RAG_FILTER_BY_DOC_TYPE=True,
            RAG_ENABLE_RERANKING=False,
            RAG_STRICT_ABSTAIN=True,
            RAG_MIN_RETRIEVED_CHUNKS=1,
            RAG_MIN_STRONG_SIMILARITY=0.60,
            RAG_OPERATIONAL_SIMILARITY_MARGIN=0.0,
            RAG_ENABLE_BUSINESS_RULES=False,
        ), patch(
            "rag._reformulate_query_with_history",
            return_value="a configuracao desconhecida existe?",
        ), patch(
            "rag._classify_query_intent",
            return_value={
                "intent": "configuration",
                "modules": ["financeiro_pagamentos"],
                "doc_types": ["md"],
            },
        ), patch(
            "rag._search_feedback_memory_chunks",
            return_value=[],
        ), patch(
            "rag.search_relevant_sections",
            return_value=[],
        ), patch(
            "rag.search_similar_chunks",
            side_effect=search,
        ) as search_mock, patch(
            "rag._ask_model",
        ) as ask_model_mock:
            answer, _returned_chunks, trace = rag.ask(
                "A configuracao desconhecida existe?"
            )

        self.assertTrue(answer.startswith(config.NO_ANSWER_PHRASE))
        self.assertTrue(trace["abstained"])
        self.assertEqual(len(search_mock.call_args_list), 2)
        self.assertEqual(ask_model_mock.call_count, 0)
        self.assertEqual(
            trace["retrieval_scope"]["additional_database_searches"],
            1,
        )
        self.assertEqual(
            trace["retrieval_scope"]["max_additional_database_searches"],
            1,
        )
        self.assertEqual(trace["query_plan_fallback"], "skipped_retrieval_budget")
        self.assertEqual(
            trace["retrieval_scope"]["strict_abstain_fallback"]["status"],
            "skipped_retrieval_budget",
        )

    def test_filtered_scope_keeps_absence_policy_when_challenger_is_disabled(self):
        model_answer = (
            "As configuracoes do MaxPag estao descritas no documento.\n\n"
            "Fontes:\n"
            "- 12-MAXPAG.md"
        )

        with patch.multiple(
            config,
            FULL_CONTEXT_ENABLED=False,
            RAG_ENABLE_GLOBAL_CHALLENGER=False,
            RAG_FILTER_BY_MODULE=True,
            RAG_FILTER_BY_DOC_TYPE=True,
            RAG_ENABLE_RERANKING=False,
            RAG_STRICT_ABSTAIN=True,
            RAG_MIN_RETRIEVED_CHUNKS=1,
            RAG_MIN_STRONG_SIMILARITY=0.60,
            RAG_OPERATIONAL_SIMILARITY_MARGIN=0.0,
            RAG_ENABLE_BUSINESS_RULES=False,
            RAG_ENABLE_GROUNDING_VALIDATION=True,
            RAG_REQUIRE_SOURCES_SECTION=True,
            RAG_MAX_REGEN_ATTEMPTS=0,
        ), patch(
            "rag._reformulate_query_with_history",
            return_value="configuracoes do maxpag",
        ), patch(
            "rag._classify_query_intent",
            return_value={
                "intent": "configuration",
                "modules": ["financeiro_pagamentos"],
                "doc_types": ["md"],
            },
        ), patch(
            "rag._search_feedback_memory_chunks",
            return_value=[],
        ), patch(
            "rag.search_relevant_sections",
            return_value=[],
        ), patch(
            "rag.search_similar_chunks",
            return_value=[copy.deepcopy(self.filtered_maxpag)],
        ) as search_mock, patch(
            "rag._ask_model",
            return_value=model_answer,
        ) as ask_model_mock:
            answer, _returned_chunks, trace = rag.ask("Como configurar o MaxPag?")

        self.assertEqual(answer, model_answer)
        self.assertEqual(search_mock.call_count, 1)
        self.assertFalse(trace["retrieval_scope"]["relaxation_attempted"])
        self.assertEqual(trace["retrieval_scope"]["preferred_scope"], "filtered")
        system = ask_model_mock.call_args.kwargs["system"]
        self.assertIn("<module_relaxation_policy>", system)
        self.assertIn("esta desativada", system)


class TestCorrectionWorkflowIntegration(unittest.TestCase):
    def test_submit_approve_publish_and_retrieve_feedback_chunk(self):
        state = {
            "feedback_items": {},
            "feedback_chunks": {},
        }

        def fake_db_call(function_name, params, expect_rows=True):
            if function_name == "ensure_embedding_index_identity":
                return []

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
