import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import bot
import config
import db
import ingest
import rag


class TestIncrementalIngest(unittest.TestCase):
    def test_same_processing_skips_embeddings_and_changed_content_updates(self):
        stored_document = {}

        def select_rows(table, select="*", filters=None, **_kwargs):
            if table == "documents" and "filename" in (filters or {}):
                return [stored_document] if stored_document else []
            if table == "documents" and "processing_hash" in (filters or {}):
                return []
            return []

        def replace_document(**kwargs):
            stored_document.clear()
            stored_document.update(kwargs["document_row"])
            return True

        embedding = [0.001] * config.EMBEDDING_DIMENSIONS
        with (
            patch.object(ingest, "supabase_select", side_effect=select_rows),
            patch.object(ingest, "supabase_update"),
            patch.object(ingest, "_document_sections_supported", return_value=False),
            patch.object(ingest, "ensure_embedding_index_identity"),
            patch.object(ingest, "_embed_batch_with_retry", return_value=[embedding]) as embed,
            patch.object(ingest, "_replace_document_atomically", side_effect=replace_document),
            patch.object(ingest, "_save_failed_report_entry"),
            patch.object(config, "CONTEXTUAL_RETRIEVAL_ENABLED", False),
        ):
            first = ingest._ingest_text_source(
                filename="manual.md",
                title="Manual",
                source="/docs/manual.md",
                doc_type="md",
                text="# Manual\n\nVersao um",
                source_type="file",
                force=False,
            )
            unchanged = ingest._ingest_text_source(
                filename="manual.md",
                title="Manual",
                source="/novo-caminho/manual.md",
                doc_type="md",
                text="# Manual\n\nVersao um",
                source_type="file",
                force=False,
            )
            changed = ingest._ingest_text_source(
                filename="manual.md",
                title="Manual",
                source="/novo-caminho/manual.md",
                doc_type="md",
                text="# Manual\n\nVersao dois",
                source_type="file",
                force=False,
            )

        self.assertEqual(first["chunks_count"], 1)
        self.assertTrue(unchanged["unchanged"])
        self.assertEqual(changed["chunks_count"], 1)
        self.assertEqual(embed.call_count, 2)
        self.assertNotEqual(first["content_hash"], changed["content_hash"])

    def test_force_reprocesses_even_when_hash_is_unchanged(self):
        text = "# Manual\n\nConteudo"
        stored_document = {}

        def select_rows(table, select="*", filters=None, **_kwargs):
            if table == "documents" and "filename" in (filters or {}):
                return [stored_document] if stored_document else []
            return []

        def replace_document(**kwargs):
            stored_document.clear()
            stored_document.update(kwargs["document_row"])
            return True

        embedding = [0.001] * config.EMBEDDING_DIMENSIONS
        with (
            patch.object(ingest, "supabase_select", side_effect=select_rows),
            patch.object(ingest, "_document_sections_supported", return_value=False),
            patch.object(ingest, "ensure_embedding_index_identity"),
            patch.object(ingest, "_embed_batch_with_retry", return_value=[embedding]) as embed,
            patch.object(ingest, "_replace_document_atomically", side_effect=replace_document),
            patch.object(ingest, "_save_failed_report_entry"),
            patch.object(config, "CONTEXTUAL_RETRIEVAL_ENABLED", False),
        ):
            ingest._ingest_text_source(
                filename="manual.md",
                title="Manual",
                source="manual.md",
                doc_type="md",
                text=text,
                source_type="file",
                force=False,
            )
            forced = ingest._ingest_text_source(
                filename="manual.md",
                title="Manual",
                source="manual.md",
                doc_type="md",
                text=text,
                source_type="file",
                force=True,
            )

        self.assertEqual(forced["chunks_count"], 1)
        self.assertEqual(embed.call_count, 2)

    def test_identical_preprocessing_reuses_embeddings_for_another_origin(self):
        cloned_chunks = [{"chunk_index": 0, "content": "Conteudo"}]
        with (
            patch.object(ingest, "supabase_select", return_value=[]),
            patch.object(
                ingest,
                "_find_reusable_document",
                return_value={"id": "source-doc", "filename": "origem.md"},
            ),
            patch.object(
                ingest,
                "_clone_prepared_rows",
                return_value=([], cloned_chunks),
            ),
            patch.object(ingest, "_document_sections_supported", return_value=False),
            patch.object(ingest, "_prepare_chunk_rows") as prepare_chunks,
            patch.object(ingest, "_replace_document_atomically", return_value=True),
            patch.object(ingest, "_save_failed_report_entry"),
            patch.object(config, "CONTEXTUAL_RETRIEVAL_ENABLED", False),
        ):
            result = ingest._ingest_text_source(
                filename="copia.md",
                title="Copia",
                source="/docs/copia.md",
                doc_type="md",
                text="# Copia\n\nConteudo",
                source_type="file",
                force=False,
            )

        self.assertTrue(result["reused_embeddings"])
        prepare_chunks.assert_not_called()

    def test_recursive_collection_never_sends_backups_to_parser(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "base.md").write_text("ativo", encoding="utf-8")
            (root / "ativos").mkdir()
            (root / "ativos" / "sub.md").write_text("ativo", encoding="utf-8")
            (root / "docbkp").mkdir()
            (root / "docbkp" / "igual.md").write_text("ativo", encoding="utf-8")
            (root / "docbkp" / "antigo.md").write_text("antigo", encoding="utf-8")

            with patch.object(ingest, "ingest_file", return_value={}) as ingest_file:
                ingest.ingest_directory(directory=str(root), recursive=True)

        processed = sorted(
            Path(call.args[0]).relative_to(root).as_posix()
            for call in ingest_file.call_args_list
        )
        self.assertEqual(processed, ["ativos/sub.md", "base.md"])

    def test_content_hash_normalizes_only_line_endings(self):
        self.assertEqual(
            ingest._content_hash("linha 1\r\nlinha 2\r\n"),
            ingest._content_hash("linha 1\nlinha 2\n"),
        )
        self.assertNotEqual(
            ingest._content_hash("linha 1\nlinha 2"),
            ingest._content_hash("linha 1\nlinha alterada"),
        )


class TestIngestCommand(unittest.IsolatedAsyncioTestCase):
    async def test_reindex_command_forces_ingestion(self):
        class TypingContext:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

        ctx = SimpleNamespace(
            reply=AsyncMock(),
            typing=Mock(return_value=TypingContext()),
        )
        with patch("bot.asyncio.to_thread", new=AsyncMock(return_value=[])) as to_thread:
            await bot.cmd_ingerir.callback(ctx)

        to_thread.assert_awaited_once_with(bot.ingest_directory, force=True)


class TestContentDeduplication(unittest.TestCase):
    def test_retrieval_collapses_exact_content_from_distinct_origins(self):
        chunks = [
            {
                "id": "chunk-a",
                "filename": "origem-a.md",
                "chunk_index": 0,
                "content": "Mesmo conteudo",
                "metadata": {"content_hash": "hash-igual"},
            },
            {
                "id": "chunk-b",
                "filename": "origem-b.md",
                "chunk_index": 0,
                "content": "Mesmo conteudo",
                "metadata": {"content_hash": "hash-igual"},
            },
        ]

        self.assertEqual(rag._dedupe_chunks(chunks), [chunks[0]])


@unittest.skipUnless(
    os.getenv("RUN_DB_INTEGRATION_TESTS") == "1",
    "requer PostgreSQL real e RUN_DB_INTEGRATION_TESTS=1",
)
class TestIncrementalIngestPostgres(unittest.TestCase):
    def setUp(self):
        suffix = uuid4().hex
        self.first_filename = f"__bm14_{suffix}/origem/manual.md"
        self.second_filename = f"__bm14_{suffix}/copia/manual.md"

    def tearDown(self):
        with db.db_transaction() as connection:
            for filename in (self.first_filename, self.second_filename):
                db.db_delete(
                    "documents",
                    {"filename": f"eq.{filename}"},
                    connection=connection,
                )
        ingest._document_sections_available = None

    @staticmethod
    def _embedding_batch(contents, *_args, **_kwargs):
        return [
            [0.001] * config.EMBEDDING_DIMENSIONS
            for _content in contents
        ]

    def test_duplicate_origin_reuses_vectors_and_keeps_both_provenances(self):
        unique_text = f"# Manual\n\nConteudo identico {uuid4().hex}"
        common_kwargs = {
            "title": "Manual",
            "doc_type": "md",
            "text": unique_text,
            "source_type": "file",
            "force": False,
        }
        with (
            patch.object(ingest, "_document_sections_supported", return_value=False),
            patch.object(ingest, "ensure_embedding_index_identity"),
            patch.object(ingest, "_save_failed_report_entry"),
            patch.object(config, "CONTEXTUAL_RETRIEVAL_ENABLED", False),
            patch.object(
                ingest,
                "_embed_batch_with_retry",
                side_effect=self._embedding_batch,
            ) as embed,
        ):
            first = ingest._ingest_text_source(
                filename=self.first_filename,
                source=f"/docs/{self.first_filename}",
                **common_kwargs,
            )
            second = ingest._ingest_text_source(
                filename=self.second_filename,
                source=f"/docs/{self.second_filename}",
                **common_kwargs,
            )

        documents = db.db_select(
            "documents",
            columns="filename,content_hash,processing_hash,chunk_count",
            filters={
                "content_hash": f"eq.{first['content_hash']}",
                "order": "filename.asc",
            },
        )
        matching = [
            document
            for document in documents
            if document["filename"] in {self.first_filename, self.second_filename}
        ]
        self.assertEqual(embed.call_count, 1)
        self.assertTrue(second["reused_embeddings"])
        self.assertEqual(len(matching), 2)
        self.assertEqual({row["chunk_count"] for row in matching}, {1})
        self.assertEqual(
            {row["processing_hash"] for row in matching},
            {first["processing_hash"]},
        )


if __name__ == "__main__":
    unittest.main()
