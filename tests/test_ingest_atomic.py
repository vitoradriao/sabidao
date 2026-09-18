import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import config
import db
import ingest


class TestAtomicIngestUnit(unittest.TestCase):
    def test_embedding_failure_does_not_start_document_replacement(self):
        with (
            patch.object(ingest, "supabase_select", return_value=[{"id": "old-doc"}]),
            patch.object(ingest, "_document_sections_supported", return_value=False),
            patch.object(ingest, "_embed_batch_with_retry", side_effect=RuntimeError("embedding falhou")),
            patch.object(ingest, "_replace_document_atomically") as replace_mock,
            patch.object(ingest, "_save_failed_report_entry"),
            patch.object(config, "CONTEXTUAL_RETRIEVAL_ENABLED", False),
        ):
            result = ingest._ingest_text_source(
                filename="atomic.md",
                title="Atomic",
                source="atomic.md",
                doc_type="md",
                text="# Versao nova\n\nConteudo novo",
                source_type="file",
                force=True,
            )

        self.assertEqual(result["error"], "preparacao incompleta")
        self.assertEqual(result["failed_chunks"], 1)
        replace_mock.assert_not_called()


@unittest.skipUnless(
    os.getenv("RUN_DB_INTEGRATION_TESTS") == "1",
    "requer PostgreSQL real e RUN_DB_INTEGRATION_TESTS=1",
)
class TestAtomicIngestPostgres(unittest.TestCase):
    def setUp(self):
        db.validate_database_config()
        self.filename = f"__bm04_atomic_{uuid4().hex}.md"
        self.old_doc_id = str(uuid4())
        with db.db_transaction() as connection:
            db.db_insert(
                "documents",
                {
                    "id": self.old_doc_id,
                    "filename": self.filename,
                    "title": "Versao anterior",
                    "source": self.filename,
                    "doc_type": "md",
                    "chunk_count": 1,
                    "priority": 5,
                },
                connection=connection,
            )
            db.db_insert(
                "document_chunks",
                {
                    "document_id": self.old_doc_id,
                    "content": "conteudo-anterior",
                    "chunk_index": 0,
                    "metadata": {"filename": self.filename},
                    "embedding": ingest.embedding_to_pgvector(self._embedding_vector()),
                    "token_count": 1,
                },
                connection=connection,
            )

    def tearDown(self):
        with db.db_transaction() as connection:
            db.db_delete(
                "documents",
                {"filename": f"eq.{self.filename}"},
                connection=connection,
            )
        ingest._document_sections_available = None

    @staticmethod
    def _embedding_vector() -> list[float]:
        return [0.001] * config.EMBEDDING_DIMENSIONS

    def _embedding_batch(self, contents, *_args, **_kwargs):
        return [self._embedding_vector() for _content in contents]

    def _ingest(self, text: str, title: str = "Versao nova") -> dict:
        return ingest._ingest_text_source(
            filename=self.filename,
            title=title,
            source=self.filename,
            doc_type="md",
            text=text,
            source_type="file",
            force=True,
        )

    def _stored_document(self) -> tuple[dict, list[dict]]:
        documents = db.db_select(
            "documents",
            filters={"filename": f"eq.{self.filename}"},
        )
        self.assertEqual(len(documents), 1)
        chunks = db.db_select(
            "document_chunks",
            filters={"document_id": f"eq.{documents[0]['id']}"},
            order_by="chunk_index.asc",
        )
        return documents[0], chunks

    def test_parsing_failure_keeps_previous_version(self):
        with patch.object(
            ingest,
            "_split_markdown_sections",
            side_effect=RuntimeError("falha simulada no parsing"),
        ):
            with self.assertRaisesRegex(RuntimeError, "falha simulada no parsing"):
                self._ingest("# Versao invalida")

        document, chunks = self._stored_document()
        self.assertEqual(str(document["id"]), self.old_doc_id)
        self.assertEqual([chunk["content"] for chunk in chunks], ["conteudo-anterior"])

    def test_embedding_failure_keeps_previous_version(self):
        with (
            patch.object(ingest, "_document_sections_supported", return_value=False),
            patch.object(
                ingest,
                "_embed_batch_with_retry",
                side_effect=RuntimeError("falha simulada no embedding"),
            ),
            patch.object(ingest, "_save_failed_report_entry"),
            patch.object(config, "CONTEXTUAL_RETRIEVAL_ENABLED", False),
        ):
            result = self._ingest("# Versao nova\n\nconteudo-novo")

        document, chunks = self._stored_document()
        self.assertEqual(result["error"], "preparacao incompleta")
        self.assertEqual(str(document["id"]), self.old_doc_id)
        self.assertEqual([chunk["content"] for chunk in chunks], ["conteudo-anterior"])

    def test_insertion_failure_rolls_back_and_keeps_previous_version(self):
        real_insert = ingest.supabase_insert

        def fail_chunk_insert(table, data, *, connection=None):
            if table == "document_chunks":
                raise RuntimeError("falha simulada ao inserir chunks")
            return real_insert(table, data, connection=connection)

        with (
            patch.object(ingest, "_embed_batch_with_retry", side_effect=self._embedding_batch),
            patch.object(ingest, "supabase_insert", side_effect=fail_chunk_insert),
            patch.object(ingest, "_save_failed_report_entry"),
            patch.object(config, "CONTEXTUAL_RETRIEVAL_ENABLED", False),
        ):
            result = self._ingest("# Versao nova\n\nconteudo-novo")

        document, chunks = self._stored_document()
        self.assertEqual(result["error"], "falha ao substituir documento")
        self.assertEqual(str(document["id"]), self.old_doc_id)
        self.assertEqual([chunk["content"] for chunk in chunks], ["conteudo-anterior"])
        self.assertEqual(
            db.db_select(
                "document_sections",
                filters={"document_id": f"eq.{self.old_doc_id}"},
            ),
            [],
        )

    def test_success_replaces_the_complete_previous_version(self):
        with (
            patch.object(ingest, "_embed_batch_with_retry", side_effect=self._embedding_batch),
            patch.object(ingest, "_save_failed_report_entry"),
            patch.object(config, "CONTEXTUAL_RETRIEVAL_ENABLED", False),
        ):
            result = self._ingest("# Versao nova\n\nconteudo-novo")

        document, chunks = self._stored_document()
        self.assertEqual(result["chunks_count"], 1)
        self.assertNotEqual(str(document["id"]), self.old_doc_id)
        self.assertEqual(document["chunk_count"], 1)
        self.assertEqual(len(chunks), 1)
        self.assertIn("conteudo-novo", chunks[0]["content"])
        self.assertNotIn("conteudo-anterior", chunks[0]["content"])
        sections = db.db_select(
            "document_sections",
            filters={"document_id": f"eq.{document['id']}"},
        )
        self.assertGreaterEqual(len(sections), 1)
        self.assertIn(chunks[0]["section_id"], {section["id"] for section in sections})

    def test_concurrent_forced_reindexes_are_serialized_without_mixing_versions(self):
        barrier = Barrier(2)

        def synchronized_embeddings(contents, *_args, **_kwargs):
            barrier.wait(timeout=10)
            return [self._embedding_vector() for _content in contents]

        with (
            patch.object(ingest, "_document_sections_supported", return_value=False),
            patch.object(ingest, "_embed_batch_with_retry", side_effect=synchronized_embeddings),
            patch.object(ingest, "_save_failed_report_entry"),
            patch.object(config, "CONTEXTUAL_RETRIEVAL_ENABLED", False),
            ThreadPoolExecutor(max_workers=2) as executor,
        ):
            futures = [
                executor.submit(self._ingest, "# Versao A\n\nconteudo-a", "Versao A"),
                executor.submit(self._ingest, "# Versao B\n\nconteudo-b", "Versao B"),
            ]
            results = [future.result(timeout=20) for future in futures]

        document, chunks = self._stored_document()
        self.assertTrue(all("error" not in result for result in results))
        self.assertEqual(len(chunks), 1)
        self.assertEqual(document["chunk_count"], 1)
        final_content = chunks[0]["content"]
        self.assertTrue("conteudo-a" in final_content or "conteudo-b" in final_content)
        self.assertFalse("conteudo-a" in final_content and "conteudo-b" in final_content)


if __name__ == "__main__":
    unittest.main()
