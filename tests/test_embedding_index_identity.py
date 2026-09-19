import os
import unittest
from pathlib import Path
from unittest.mock import patch

import config
import db
import ingest
import rag


ROOT = Path(__file__).resolve().parents[1]


class TestEmbeddingDimensionContract(unittest.TestCase):
    def test_rejects_short_and_long_embeddings_without_adjusting_them(self):
        with patch.object(config, "EMBEDDING_DIMENSIONS", 3):
            with self.assertRaisesRegex(ValueError, "esperado 3, obtido 2"):
                rag._normalize_embedding([0.1, 0.2])
            with self.assertRaisesRegex(ValueError, "esperado 3, obtido 4"):
                rag.embedding_to_pgvector([0.1, 0.2, 0.3, 0.4])


class TestEmbeddingIdentityContract(unittest.TestCase):
    def setUp(self):
        rag._validated_embedding_index_identities.clear()
        rag._query_embedding_cache._cache.clear()

    def tearDown(self):
        rag._validated_embedding_index_identities.clear()
        rag._query_embedding_cache._cache.clear()

    def test_registers_complete_identity_for_each_scope(self):
        with (
            patch.multiple(
                config,
                EMBEDDING_PROVIDER="openai",
                EMBEDDING_MODEL="embedding-model",
                EMBEDDING_DIMENSIONS=1536,
                EMBEDDING_PREPROCESSING_VERSION="preprocess-v2",
            ),
            patch("rag.supabase_rpc", return_value=[]) as rpc_mock,
        ):
            for scope in ("corpus", "sections", "feedback"):
                rag.ensure_embedding_index_identity(scope)

        self.assertEqual(rpc_mock.call_count, 3)
        for scope, call in zip(("corpus", "sections", "feedback"), rpc_mock.call_args_list):
            self.assertEqual(call.args[0], "ensure_embedding_index_identity")
            self.assertEqual(
                call.args[1],
                {
                    "p_index_scope": scope,
                    "p_provider": "openai",
                    "p_model": "embedding-model",
                    "p_dimensions": 1536,
                    "p_preprocessing_version": "preprocess-v2",
                },
            )

    def test_model_change_with_same_dimension_is_revalidated(self):
        with (
            patch.multiple(
                config,
                EMBEDDING_PROVIDER="gemini",
                EMBEDDING_MODEL="model-a",
                EMBEDDING_DIMENSIONS=1536,
                EMBEDDING_PREPROCESSING_VERSION="rag-text-v1",
            ),
            patch(
                "rag.supabase_rpc",
                side_effect=[[], RuntimeError("identidade incompativel")],
            ) as rpc_mock,
        ):
            rag.ensure_embedding_index_identity("corpus")
            with patch.object(config, "EMBEDDING_MODEL", "model-b"):
                with self.assertRaisesRegex(RuntimeError, "identidade incompativel"):
                    rag.ensure_embedding_index_identity("corpus")

        self.assertEqual(rpc_mock.call_count, 2)

    def test_query_embedding_cache_is_invalidated_by_identity(self):
        with (
            patch.multiple(
                config,
                EMBEDDING_PROVIDER="gemini",
                EMBEDDING_MODEL="model-a",
                EMBEDDING_DIMENSIONS=3,
                EMBEDDING_PREPROCESSING_VERSION="rag-text-v1",
            ),
            patch("rag.create_query_embedding", side_effect=[[0.1] * 3, [0.2] * 3]) as create_mock,
        ):
            first = rag._get_cached_query_embedding("consulta")
            repeated = rag._get_cached_query_embedding("consulta")
            with patch.object(config, "EMBEDDING_MODEL", "model-b"):
                after_model_change = rag._get_cached_query_embedding("consulta")

        self.assertEqual(first, repeated)
        self.assertNotEqual(first, after_model_change)
        self.assertEqual(create_mock.call_count, 2)


class TestEmbeddingIdentityOperationGuards(unittest.TestCase):
    @staticmethod
    def _section(*, section_id: str | None = "section-1") -> ingest.AnalyticalSection:
        return ingest.AnalyticalSection(
            section_index=0,
            title="Secao",
            heading_path="Documento > Secao",
            content="Conteudo",
            module="geral",
            answer_mode="general",
            entities={},
            semantic_context="Contexto",
            section_id=section_id,
        )

    def test_document_ingest_checks_identity_before_embedding(self):
        section = self._section()
        with (
            patch("ingest.ensure_embedding_index_identity", side_effect=RuntimeError("corpus incompativel")),
            patch("ingest._embed_batch_with_retry") as embed_mock,
        ):
            with self.assertRaisesRegex(RuntimeError, "corpus incompativel"):
                ingest._prepare_chunk_rows(
                    doc_id="doc-1",
                    filename="documento.md",
                    title="Documento",
                    text="# Documento",
                    doc_type="md",
                    source_type="file",
                    module="geral",
                    doc_priority=5,
                    chunk_items=[(0, "Conteudo", "Conteudo", section)],
                )

        embed_mock.assert_not_called()

    def test_section_ingest_checks_identity_before_embedding(self):
        with (
            patch("ingest.ensure_embedding_index_identity", side_effect=RuntimeError("secoes incompativeis")),
            patch("ingest._embed_batch_with_retry") as embed_mock,
        ):
            with self.assertRaisesRegex(RuntimeError, "secoes incompativeis"):
                ingest._prepare_section_retrieval_data("Documento", [self._section()])

        embed_mock.assert_not_called()

    def test_feedback_publish_checks_identity_before_embedding(self):
        item = {
            "id": "feedback-1",
            "query": "Pergunta",
            "corrected_answer": "Resposta",
            "scope": {"level": "global"},
            "status": "APPROVED",
            "tags": [],
        }
        with (
            patch("rag.supabase_select", return_value=[item]),
            patch("rag.ensure_embedding_index_identity", side_effect=RuntimeError("feedback incompativel")),
            patch("rag.create_document_embedding") as embed_mock,
        ):
            with self.assertRaisesRegex(RuntimeError, "feedback incompativel"):
                rag.publish_feedback_item("feedback-1")

        embed_mock.assert_not_called()

    def test_searches_check_identity_before_creating_query_embedding(self):
        search_calls = (
            ("corpus", lambda: rag.search_similar_chunks("consulta")),
            ("sections", lambda: rag.search_relevant_sections("consulta")),
            ("feedback", lambda: rag._search_feedback_memory_chunks("consulta")),
        )

        with patch.object(config, "SECTION_RETRIEVAL_ENABLED", True):
            for scope, search in search_calls:
                with (
                    self.subTest(scope=scope),
                    patch(
                        "rag.ensure_embedding_index_identity",
                        side_effect=RuntimeError(f"{scope} incompativel"),
                    ),
                    patch("rag._get_cached_query_embedding") as embed_mock,
                ):
                    with self.assertRaisesRegex(RuntimeError, f"{scope} incompativel"):
                        search()
                    embed_mock.assert_not_called()


@unittest.skipUnless(
    os.getenv("RUN_DB_INTEGRATION_TESTS") == "1",
    "requer PostgreSQL real e RUN_DB_INTEGRATION_TESTS=1",
)
class TestEmbeddingIdentityPostgres(unittest.TestCase):
    def test_migration_and_identity_contract(self):
        db.validate_database_config()
        if db.psycopg is None:
            self.skipTest("psycopg nao instalado")

        migration = (ROOT / "sql" / "add_embedding_index_identity.sql").read_text(
            encoding="utf-8"
        )
        fixture = (
            ROOT / "tests" / "postgres" / "embedding_index_identity_fixture.sql"
        ).read_text(encoding="utf-8")

        with db.psycopg.connect(db.get_database_url(), autocommit=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute(migration)
                while cursor.nextset():
                    pass
                cursor.execute(fixture)
                while cursor.nextset():
                    pass


if __name__ == "__main__":
    unittest.main()
