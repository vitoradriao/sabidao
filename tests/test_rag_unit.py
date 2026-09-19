import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

import config
import db
import rag
from bot_common import normalize_over_numbered_response


class TestIntentRouting(unittest.TestCase):
    def test_sql_lookup_intent_for_maxpedido_tables(self):
        query = "Preciso de select na tabela mxsintegracaopedido para analisar erro"
        plan = rag._classify_query_intent(query)

        self.assertEqual(plan["intent"], "sql_lookup")
        self.assertIn("sql_integracao", plan["modules"])

    def test_configuration_intent_for_usagrade(self):
        query = "Como configurar o parametro USAGRADE na rotina ParamFilial"
        plan = rag._classify_query_intent(query)

        self.assertEqual(plan["intent"], "configuration")
        self.assertIn("parametros_configuracao", plan["modules"])

    def test_configuration_intent_accepts_controlled_portuguese_variants(self):
        variants = (
            "parâmetro",
            "parametro",
            "parâmetros",
            "parametros",
            "parametrização",
            "parametrizacao",
        )

        for variant in variants:
            with self.subTest(variant=variant):
                plan = rag._classify_query_intent(f"Como consultar {variant} do maxPedido?")

                self.assertEqual(plan["intent"], "configuration")
                self.assertIn("parametros_configuracao", plan["modules"])

    def test_account_current_variants_keep_configuration_and_financial_modules(self):
        variants = ("CC", "C/C", "Conta Corrente", "Flex")

        for variant in variants:
            with self.subTest(variant=variant):
                plan = rag._classify_query_intent(f"Quais parâmetros existem para {variant}?")

                self.assertEqual(plan["intent"], "configuration")
                self.assertIn("parametros_configuracao", plan["modules"])
                self.assertIn("financeiro_pagamentos", plan["modules"])

    def test_account_current_reproduction_exposes_routing_signals(self):
        plan = rag._classify_query_intent(
            "parâmetros relacionados a conta corrente do maxpedido"
        )

        self.assertEqual(plan["intent"], "configuration")
        self.assertEqual(
            plan["modules"],
            ["financeiro_pagamentos", "parametros_configuracao"],
        )
        self.assertIn(
            "parametros",
            plan["routing_signals"]["intent_keywords"]["configuration"],
        )
        self.assertIn(
            "conta corrente",
            plan["routing_signals"]["module_keywords"]["financeiro_pagamentos"],
        )

    def test_route_terms_do_not_match_substrings(self):
        plan = rag._classify_query_intent("Como funciona a parametria comercial?")

        self.assertEqual(plan["intent"], "general")
        self.assertNotIn("parametros_configuracao", plan["modules"])

    def test_maxpag_keeps_financial_module_without_account_current_signal(self):
        plan = rag._classify_query_intent("Como funciona o saldo no MaxPag?")

        self.assertIn("financeiro_pagamentos", plan["modules"])
        self.assertNotIn("parametros_configuracao", plan["modules"])
        self.assertEqual(
            plan["routing_signals"]["module_keywords"]["financeiro_pagamentos"],
            ["maxpag"],
        )

    def test_preprocess_preserves_technical_identifier_for_fts(self):
        query = "Como configurar CON_USACREDRCA no maxPedido?"

        _embedding_query, fts_query = rag._preprocess_query(query)

        self.assertEqual(fts_query, query)
        self.assertIn("CON_USACREDRCA", fts_query)


class TestPromptFormatting(unittest.TestCase):
    def test_runtime_prompt_discourages_over_numbered_answers(self):
        self.assertIn("Nao transforme toda a resposta em lista numerada", config.SYSTEM_PROMPT)


    def test_troubleshooting_instruction_limits_checklist_to_verifications(self):
        instruction = rag._intent_response_instruction({"intent": "troubleshooting"})

        self.assertIn("Use checklist apenas nas verificacoes praticas", instruction)


class TestDiscordResponseFormatting(unittest.TestCase):
    def test_normalizes_pathological_numbered_sections(self):
        answer = (
            "7. Portfolio recomendado/mix.\n"
            "8. Status de fechamento e autorizacoes comerciais.\n"
            "9. Filtragem de produtos pela aba Tabela.\n\n"
            "4. Como diagnosticar quando nao funciona\n"
            "5. Confirmar se `HABILITA_SISTEMA_GERA` esta ativo.\n"
            "6. Validar `CNPJ_SISTEMA_GERA`.\n\n"
            "5. Quando acionar suporte\n"
            "6. Acionar Suporte Gera:\n"
            "7. Hub Gera fora do ar.\n"
            "8. Erro de autenticacao ou token.\n\n"
            "Fontes:\n"
            "1. MS-HUB-GERA-MAXPEDIDO.md"
        )

        normalized = normalize_over_numbered_response(answer)

        self.assertNotRegex(normalized, r"(?m)^\d+[.)]\s")
        self.assertIn("**Como diagnosticar quando nao funciona**", normalized)
        self.assertIn("- Confirmar se `HABILITA_SISTEMA_GERA` esta ativo.", normalized)
        self.assertIn("- MS-HUB-GERA-MAXPEDIDO.md", normalized)

    def test_preserves_short_step_by_step(self):
        answer = (
            "1. Abra a Central de Configuracoes.\n"
            "2. Habilite o parametro documentado.\n"
            "3. Reinicie o extrator."
        )

        self.assertEqual(normalize_over_numbered_response(answer), answer)


class TestStrictAbstain(unittest.TestCase):
    def test_abstains_when_similarity_is_weak(self):
        chunks = [{"similarity": 0.42, "filename": "doc.md"}]
        with patch.multiple(
            config,
            RAG_STRICT_ABSTAIN=True,
            RAG_MIN_RETRIEVED_CHUNKS=1,
            RAG_MIN_STRONG_SIMILARITY=0.62,
            RAG_OPERATIONAL_SIMILARITY_MARGIN=0.05,
        ):
            abstain, reason = rag._should_strict_abstain("Como configurar parametro?", chunks)

        self.assertTrue(abstain)
        self.assertEqual(reason, "low_similarity")

    def test_abstains_when_too_few_chunks(self):
        chunks = [{"similarity": 0.90, "filename": "doc.md"}]
        with patch.multiple(
            config,
            RAG_STRICT_ABSTAIN=True,
            RAG_MIN_RETRIEVED_CHUNKS=2,
            RAG_MIN_STRONG_SIMILARITY=0.62,
            RAG_OPERATIONAL_SIMILARITY_MARGIN=0.05,
        ):
            abstain, reason = rag._should_strict_abstain("Fluxo de pedido", chunks)

        self.assertTrue(abstain)
        self.assertEqual(reason, "few_chunks")

    def test_does_not_treat_fusion_or_feedback_priority_as_confidence(self):
        chunks = [
            {
                "similarity": 0.20,
                "vector_similarity": 0.20,
                "fusion_score": 0.99,
                "feedback_priority": 2,
                "filename": "exact.md",
            }
        ]
        with patch.multiple(
            config,
            RAG_STRICT_ABSTAIN=True,
            RAG_MIN_RETRIEVED_CHUNKS=1,
            RAG_MIN_STRONG_SIMILARITY=0.62,
            RAG_OPERATIONAL_SIMILARITY_MARGIN=0.0,
        ):
            abstain, reason = rag._should_strict_abstain("Pergunta geral", chunks)

        trace = rag._summarize_chunks_for_trace(chunks)
        self.assertTrue(abstain)
        self.assertEqual(reason, "low_similarity")
        self.assertEqual(trace["top_similarity"], 0.20)
        self.assertEqual(trace["top_fusion_score"], 0.99)
        self.assertEqual(trace["top_feedback_priority"], 2.0)


class TestCitationValidation(unittest.TestCase):
    def test_accepts_grounded_answer_with_inline_citations(self):
        answer = (
            "Use o parametro USAGRADE para habilitar grade [fonte: guia-maxpedido.md].\n\n"
            "Fontes:\n"
            "- guia-maxpedido.md"
        )

        valid, errors, cited = rag._validate_grounded_answer(
            answer=answer,
            allowed_sources={"guia-maxpedido.md"},
            question="Como configurar USAGRADE?",
            require_sources_section=True,
        )

        self.assertTrue(valid)
        self.assertEqual(errors, [])
        self.assertIn("guia-maxpedido.md", cited)

    def test_accepts_sources_section_without_inline_citation(self):
        answer = (
            "A tabela principal e mxsintegracaopedido.\n\n"
            "Fontes:\n"
            "- guia-maxpedido.md"
        )

        valid, errors, _ = rag._validate_grounded_answer(
            answer=answer,
            allowed_sources={"guia-maxpedido.md"},
            question="Qual tabela de integracao?",
            require_sources_section=True,
        )

        self.assertTrue(valid)
        self.assertEqual(errors, [])


class TestCitationFormatting(unittest.TestCase):
    def test_moves_inline_citations_to_sources_section(self):
        answer = (
            "Conta corrente usa parametro X [fonte: guia-maxpedido.md].\n"
            "Outro ponto [fonte: parametros.md]."
        )
        formatted, cited = rag._enforce_sources_section_only(
            answer,
            allowed_sources={"guia-maxpedido.md", "parametros.md"},
            source_display_map={
                "guia-maxpedido.md": "GUIA-MAXPEDIDO.md",
                "parametros.md": "PARAMETROS.md",
            },
        )

        self.assertNotIn("[fonte:", formatted.lower())
        self.assertIn("Fontes:", formatted)
        self.assertIn("- GUIA-MAXPEDIDO.md", formatted)
        self.assertIn("- PARAMETROS.md", formatted)
        self.assertEqual(cited, {"guia-maxpedido.md", "parametros.md"})

    def test_removes_bold_sources_section_before_rebuilding_sources(self):
        answer = (
            "O pedido precisa ser validado na integracao.\n\n"
            "**Fontes:**\n"
            "- `01-LAYOUT-INTEGRACAO.md`\n"
            "- `05-PEDIDOS-E-VENDAS.md`"
        )
        formatted, cited = rag._enforce_sources_section_only(
            answer,
            allowed_sources={"01-layout-integracao.md", "05-pedidos-e-vendas.md"},
            source_display_map={
                "01-layout-integracao.md": "01-LAYOUT-INTEGRACAO.md",
                "05-pedidos-e-vendas.md": "05-PEDIDOS-E-VENDAS.md",
            },
        )
        self.assertEqual(formatted.count("Fontes:"), 1)
        self.assertNotIn("**Fontes:**", formatted)
        self.assertIn("- 01-LAYOUT-INTEGRACAO.md", formatted)
        self.assertEqual(cited, {"01-layout-integracao.md", "05-pedidos-e-vendas.md"})


class TestAnalyticalContextFormatting(unittest.TestCase):
    def test_build_context_includes_analytical_block_before_evidence(self):
        chunks = [
            {
                "id": "chunk-1",
                "document_id": "doc-1",
                "filename": "pedidos.md",
                "content": "Validar MXSINTEGRACAOPEDIDO quando o pedido nao aparece no ERP.",
                "chunk_index": 0,
                "similarity": 0.91,
                "heading_path": "Pedidos > Pedido nao aparece no ERP",
                "semantic_context": "Secao: Pedidos > Pedido nao aparece no ERP | Tabelas: MXSINTEGRACAOPEDIDO",
                "answer_mode": "troubleshooting",
                "entities": {
                    "tables": ["MXSINTEGRACAOPEDIDO"],
                    "parameters": [],
                    "endpoints": [],
                    "statuses": [],
                    "error_terms": ["pedido nao aparece no ERP"],
                },
                "metadata": {"doc_priority": 5, "source_kind": "kb"},
            }
        ]

        context = rag.build_context(chunks)

        self.assertIn("<analytical_context>", context)
        self.assertIn("Assunto/secoes: Pedidos > Pedido nao aparece no ERP", context)
        self.assertIn("Tabelas: MXSINTEGRACAOPEDIDO", context)
        self.assertLess(context.index("<analytical_context>"), context.index("<evidence>"))

    def test_build_context_limits_document_dominance(self):
        chunks = []
        for idx in range(5):
            chunks.append(
                {
                    "id": f"doc-a-{idx}",
                    "document_id": "doc-a",
                    "section_id": "section-a",
                    "filename": "dominante.md",
                    "content": f"Trecho dominante {idx}",
                    "chunk_index": idx,
                    "similarity": 0.95 - (idx * 0.01),
                    "metadata": {"doc_priority": 5, "source_kind": "kb"},
                }
            )
        chunks.append(
            {
                "id": "doc-b-0",
                "document_id": "doc-b",
                "section_id": "section-b",
                "filename": "diverso.md",
                "content": "Trecho diverso",
                "chunk_index": 0,
                "similarity": 0.90,
                "metadata": {"doc_priority": 5, "source_kind": "kb"},
            }
        )

        with patch.multiple(config, MAX_CHUNKS_PER_SECTION=2, MAX_CHUNKS_PER_DOCUMENT=3):
            context = rag.build_context(chunks)

        self.assertIn("dominante.md", context)
        self.assertIn("diverso.md", context)
        self.assertLessEqual(context.count("Trecho dominante"), 3)


class TestBusinessRulesContext(unittest.TestCase):
    def test_loads_full_business_rules_when_limit_allows_file_size(self):
        original_cache = rag._business_rules_cache
        rag._business_rules_cache = None

        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                rules_path = Path(tmpdir) / "rules.md"
                rules_text = "regra de negocio\n" * 4000
                rules_path.write_text(rules_text, encoding="utf-8")

                with patch.multiple(
                    config,
                    RAG_ENABLE_BUSINESS_RULES=True,
                    BUSINESS_RULES_FILE=str(rules_path),
                    BUSINESS_RULES_MAX_CHARS=80000,
                ):
                    loaded = rag._load_business_rules_context()

            self.assertEqual(loaded, rules_text.strip())
        finally:
            rag._business_rules_cache = original_cache


class TestRerankPolicy(unittest.TestCase):
    def test_should_rerank_only_inside_gray_zone(self):
        chunks = [
            {"id": "1", "document_id": "doc-1", "section_id": "s-1", "filename": "a.md", "similarity": 0.70},
            {"id": "2", "document_id": "doc-2", "section_id": "s-2", "filename": "b.md", "similarity": 0.69},
            {"id": "3", "document_id": "doc-3", "section_id": "s-3", "filename": "c.md", "similarity": 0.68},
        ]
        strong_chunks = [
            {"id": "1", "document_id": "doc-1", "section_id": "s-1", "filename": "a.md", "similarity": 0.91},
            {"id": "2", "document_id": "doc-2", "section_id": "s-2", "filename": "b.md", "similarity": 0.69},
        ]

        with patch.multiple(
            config,
            RAG_ENABLE_RERANKING=True,
            RERANKER_MIN_TRIGGER_SIM=0.55,
            RERANKER_MAX_TRIGGER_SIM=0.82,
        ):
            self.assertTrue(rag._should_rerank_chunks(chunks))
            self.assertFalse(rag._should_rerank_chunks(strong_chunks))


class TestHybridRankingContract(unittest.TestCase):
    def test_preserves_rrf_order_and_lexical_only_candidate(self):
        chunks = [
            {
                "id": "lexical",
                "document_id": "doc-lexical",
                "section_id": "section-lexical",
                "filename": "lexical.md",
                "content": "Identificador exato CODIGO_XYZ.",
                "similarity": 0.20,
                "vector_similarity": 0.20,
                "lexical_score": 0.9,
                "fusion_score": 0.020,
                "retrieval_origin": "lexical",
                "retrieval_rank": 1,
                "is_neighbor": False,
            },
            {
                "id": "neighbor",
                "document_id": "doc-lexical",
                "section_id": "section-lexical",
                "filename": "lexical.md",
                "content": "Contexto adjacente.",
                "similarity": 0.98,
                "vector_similarity": 0.98,
                "lexical_score": None,
                "fusion_score": None,
                "retrieval_origin": "neighbor",
                "retrieval_rank": 1,
                "is_neighbor": True,
                "seed_chunk_id": "lexical",
            },
            {
                "id": "vector",
                "document_id": "doc-vector",
                "section_id": "section-vector",
                "filename": "vector.md",
                "content": "Resultado vetorial generico.",
                "similarity": 0.95,
                "vector_similarity": 0.95,
                "lexical_score": None,
                "fusion_score": 0.010,
                "retrieval_origin": "vector",
                "retrieval_rank": 2,
                "is_neighbor": False,
            },
        ]

        processed = rag._postprocess_search_results(
            chunks,
            max_results=3,
            threshold=0.55,
            ranking_mode="retrieval",
        )
        context = rag.build_context(processed)

        self.assertEqual(
            [chunk["id"] for chunk in processed],
            ["lexical", "neighbor", "vector"],
        )
        self.assertEqual(processed[0]["retrieval_origin"], "lexical")
        self.assertEqual(processed[1]["retrieval_origin"], "neighbor")
        self.assertIsNone(processed[1]["fusion_score"])
        self.assertLess(
            context.index('source="lexical.md"'),
            context.index('source="vector.md"'),
        )

    def test_reranker_order_reaches_context_builder(self):
        chunks = [
            {
                "id": "rrf-first",
                "document_id": "doc-rrf",
                "section_id": "section-rrf",
                "filename": "rrf.md",
                "content": "Primeiro resultado definido pelo RRF.",
                "similarity": 0.70,
                "fusion_score": 0.020,
            },
            {
                "id": "reranker-first",
                "document_id": "doc-reranker",
                "section_id": "section-reranker",
                "filename": "reranker.md",
                "content": "Resultado escolhido pelo reranker.",
                "similarity": 0.69,
                "fusion_score": 0.015,
            },
            {
                "id": "third",
                "document_id": "doc-third",
                "section_id": "section-third",
                "filename": "third.md",
                "content": "Terceiro resultado recuperado.",
                "similarity": 0.68,
                "fusion_score": 0.010,
            },
        ]
        response = type("RerankResponse", (), {"text": "1,0,2"})()

        with patch.multiple(
            config,
            RAG_ENABLE_RERANKING=True,
            RERANKER_MIN_TRIGGER_SIM=0.55,
            RERANKER_MAX_TRIGGER_SIM=0.82,
            RERANKER_MAX_CANDIDATES=8,
            MAX_CHUNKS_PER_SECTION=2,
            MAX_CHUNKS_PER_DOCUMENT=3,
        ), patch("rag._gemini_generate", return_value=response):
            reranked = rag._rerank_chunks_with_llm("pergunta", chunks)
            context = rag.build_context(reranked)

        self.assertEqual(reranked[0]["id"], "reranker-first")
        self.assertLess(
            context.index('source="reranker.md"'),
            context.index('source="rrf.md"'),
        )

    def test_vector_fallback_keeps_similarity_filter_and_order(self):
        chunks = [
            {"id": "low", "similarity": 0.20},
            {"id": "high", "similarity": 0.90},
            {"id": "middle", "similarity": 0.60},
        ]

        with patch.object(config, "SIMILARITY_FLOOR_FACTOR", 0.8):
            processed = rag._postprocess_search_results(
                chunks,
                max_results=3,
                threshold=0.55,
            )

        self.assertEqual([chunk["id"] for chunk in processed], ["high", "middle"])


class TestOpenAIExtraction(unittest.TestCase):
    def test_extracts_text_from_nested_content_item(self):
        content = [{"type": "text", "text": {"value": "Resposta final"}}]
        extracted = rag._openai_extract_text(content)
        self.assertEqual(extracted, "Resposta final")

    def test_extracts_text_from_message_refusal(self):
        extracted = rag._openai_extract_text(
            None,
            message={"role": "assistant", "content": "", "refusal": "Nao posso ajudar com isso."},
        )
        self.assertEqual(extracted, "Nao posso ajudar com isso.")

    def test_extracts_fallback_choice_text(self):
        extracted = rag._openai_extract_text(
            None,
            raw_response={"choices": [{"text": "Fallback de texto"}]},
        )
        self.assertEqual(extracted, "Fallback de texto")


class TestGroundingFallbackBehavior(unittest.TestCase):
    def test_does_not_append_retrieved_sources_when_model_omits_citations(self):
        answer = "O modulo GERA integra regras comerciais, ofertas e descontos ao maxPedido."

        with patch.multiple(
            config,
            RAG_ENABLE_GROUNDING_VALIDATION=True,
            RAG_REQUIRE_SOURCES_SECTION=True,
            RAG_MAX_REGEN_ATTEMPTS=0,
        ), patch("rag._ask_model") as ask_model_mock:
            revised_answer, errors, cited, attempts = rag._apply_grounding_regeneration(
                answer=answer,
                question="Como trabalhar com o modulo GERA?",
                system="system",
                conversation_history=[],
                images=[],
                allowed_sources={"21-ms-hub-gera-maxpedido.md", "15-fluxo-pedidos-gera.md"},
                source_display_map={
                    "21-ms-hub-gera-maxpedido.md": "21-MS-HUB-GERA-MAXPEDIDO.md",
                    "15-fluxo-pedidos-gera.md": "15-FLUXO-PEDIDOS-GERA.md",
                },
            )

        ask_model_mock.assert_not_called()
        self.assertIn("Resposta sem citacoes de fonte.", errors)
        self.assertEqual(attempts, 0)
        self.assertNotIn("Fontes:", revised_answer)
        self.assertEqual(cited, set())
        self.assertTrue(revised_answer.startswith(config.NO_ANSWER_PHRASE))

    def test_unknown_citation_is_rejected_without_leaking_source(self):
        answer = "Use o parametro inventado.\n\nFontes:\n- fonte-inexistente.md"

        with patch.multiple(
            config,
            RAG_ENABLE_GROUNDING_VALIDATION=True,
            RAG_REQUIRE_SOURCES_SECTION=True,
            RAG_MAX_REGEN_ATTEMPTS=0,
        ):
            revised_answer, errors, cited, attempts = rag._apply_grounding_regeneration(
                answer=answer,
                question="Qual parametro devo usar?",
                system="system",
                conversation_history=[],
                images=[],
                allowed_sources={"guia.md"},
            )

        self.assertTrue(revised_answer.startswith(config.NO_ANSWER_PHRASE))
        self.assertTrue(any("fonte-inexistente.md" in error for error in errors))
        self.assertEqual(cited, set())
        self.assertEqual(attempts, 0)

    def test_provider_error_skips_grounding_regeneration(self):
        provider_error = rag._provider_error_response(
            "O servico esta sobrecarregado no momento. Tente novamente em alguns segundos."
        )

        with patch.multiple(
            config,
            RAG_ENABLE_GROUNDING_VALIDATION=True,
            RAG_REQUIRE_SOURCES_SECTION=True,
            RAG_MAX_REGEN_ATTEMPTS=1,
        ), patch("rag._ask_model") as ask_model_mock:
            revised_answer, errors, cited, attempts = rag._apply_grounding_regeneration(
                answer=provider_error,
                question="Como configurar USAGRADE?",
                system="system",
                conversation_history=[],
                images=[],
                allowed_sources={"guia.md"},
            )

        ask_model_mock.assert_not_called()
        self.assertEqual(revised_answer, str(provider_error))
        self.assertEqual(errors, [])
        self.assertEqual(cited, set())
        self.assertEqual(attempts, 0)

    def test_keeps_answer_when_only_non_critical_grounding_error(self):
        answer = "Conta corrente usa este fluxo [fonte: guia.md].\n\nFontes:\n- guia.md"
        non_critical_error = ["Foram detectadas afirmacoes factuais sem citacao inline."]

        with patch.multiple(
            config,
            RAG_ENABLE_GROUNDING_VALIDATION=True,
            RAG_REQUIRE_SOURCES_SECTION=True,
            RAG_MAX_REGEN_ATTEMPTS=0,
        ), patch(
            "rag._validate_grounded_answer",
            return_value=(False, non_critical_error, {"guia.md"}),
        ):
            revised_answer, errors, cited, attempts = rag._apply_grounding_regeneration(
                answer=answer,
                question="Como trabalhar com conta corrente?",
                system="system",
                conversation_history=[],
                images=[],
                allowed_sources={"guia.md"},
            )

        self.assertNotIn("[fonte:", revised_answer.lower())
        self.assertIn("Fontes:", revised_answer)
        self.assertEqual(errors, non_critical_error)
        self.assertIn("guia.md", cited)
        self.assertEqual(attempts, 0)

    def test_abstains_when_grounding_error_is_critical(self):
        critical_error = ["Resposta vazia."]

        with patch.multiple(
            config,
            RAG_ENABLE_GROUNDING_VALIDATION=True,
            RAG_REQUIRE_SOURCES_SECTION=True,
            RAG_MAX_REGEN_ATTEMPTS=0,
        ), patch(
            "rag._validate_grounded_answer",
            return_value=(False, critical_error, set()),
        ):
            revised_answer, errors, _cited, attempts = rag._apply_grounding_regeneration(
                answer="Resposta sem fontes",
                question="Como trabalhar com conta corrente?",
                system="system",
                conversation_history=[],
                images=[],
                allowed_sources={"guia.md"},
            )

        self.assertTrue(revised_answer.startswith(config.NO_ANSWER_PHRASE))
        self.assertEqual(errors, critical_error)
        self.assertEqual(attempts, 0)


class TestDatabaseValidation(unittest.TestCase):
    def test_validate_database_accepts_env_url(self):
        with patch.dict("os.environ", {"DATABASE_URL": "postgresql://bot_maxima:bot_maxima@localhost:5432/bot_maxima"}):
            db.validate_database_config()

    def test_validate_database_rejects_missing_env_url(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(EnvironmentError):
                db.validate_database_config()


if __name__ == "__main__":
    unittest.main()
