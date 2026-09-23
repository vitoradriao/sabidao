"""Comportamento da ingestão canônica com banco e embeddings simulados."""

import copy
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from unittest.mock import patch
from uuid import uuid4

import config
import db
import ingest
from canonical_docs import CanonicalDocumentError


FIXTURE = Path(__file__).resolve().parents[1] / "contracts/canonical-docs/v1/fixtures/valid/troubleshooting.md"


class MemoryDatabase:
    def __init__(self):
        self.tables = {name: [] for name in ("documents", "document_sections", "document_chunks")}
        self.lock = RLock()
        self.fail_chunks = False
        self.lock_keys = []

    @contextmanager
    def transaction(self):
        with self.lock:
            before = copy.deepcopy(self.tables)
            try:
                yield self
            except Exception:
                self.tables = before
                raise

    def advisory_lock(self, key, *, connection):
        self.lock_keys.append(key)

    def select(self, table, select="*", filters=None, *, connection=None):
        rows = self.tables[table]
        for key, value in (filters or {}).items():
            if key in {"order", "limit"}:
                continue
            expected = value[3:] if isinstance(value, str) and value.startswith("eq.") else value
            rows = [row for row in rows if str(row.get(key)) == str(expected)]
        return copy.deepcopy(rows[: int((filters or {}).get("limit", len(rows)))])

    def insert(self, table, data, *, connection=None):
        if table == "document_chunks" and self.fail_chunks:
            raise RuntimeError("falha simulada no insert")
        rows = data if isinstance(data, list) else [data]
        self.tables[table].extend(copy.deepcopy(rows))
        return rows

    def update(self, table, data, filters, *, connection=None):
        selected = self.select(table, filters=filters)
        ids = {row["id"] for row in selected}
        for row in self.tables[table]:
            if row.get("id") in ids:
                row.update(copy.deepcopy(data))
        return selected

    def delete(self, table, column, value, *, connection=None):
        self.tables[table] = [row for row in self.tables[table] if str(row.get(column)) != str(value)]
        if table == "documents":
            for child in ("document_sections", "document_chunks"):
                self.tables[child] = [row for row in self.tables[child] if str(row.get("document_id")) != str(value)]


class CanonicalIngestTest(unittest.TestCase):
    def setUp(self):
        self.database = MemoryDatabase()
        self.text = FIXTURE.read_text(encoding="utf-8")
        self.embedding_calls = []

        def embed(contents, *_args, **_kwargs):
            self.embedding_calls.extend(contents)
            return [[0.001] * config.EMBEDDING_DIMENSIONS for _ in contents]

        self.patches = [
            patch.object(ingest, "db_transaction", self.database.transaction),
            patch.object(ingest, "db_advisory_xact_lock", self.database.advisory_lock),
            patch.object(ingest, "supabase_select", self.database.select),
            patch.object(ingest, "supabase_insert", self.database.insert),
            patch.object(ingest, "supabase_update", self.database.update),
            patch.object(ingest, "supabase_delete", self.database.delete),
            patch.object(ingest, "_document_sections_supported", return_value=True),
            patch.object(ingest, "ensure_embedding_index_identity"),
            patch.object(ingest, "_embed_batch_with_retry", side_effect=embed),
            patch.object(ingest, "_save_failed_report_entry"),
            patch.object(config, "CONTEXTUAL_RETRIEVAL_ENABLED", False),
        ]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)

    def ingest(self, text=None, filename="a.md", force=False):
        return ingest._ingest_text_source(
            filename=filename, title=Path(filename).stem, source=filename,
            doc_type="md", text=text or self.text, source_type="file", force=force,
        )

    def test_yaml_is_excluded_and_identity_is_projected(self):
        result = self.ingest()
        document = self.database.tables["documents"][0]
        sections = self.database.tables["document_sections"]
        chunks = self.database.tables["document_chunks"]
        self.assertEqual(result["chunks_count"], len(chunks))
        self.assertEqual(document["canonical_id"], "10000000-0000-4000-8000-000000000004")
        self.assertEqual(document["document_revision"], 1)
        self.assertEqual(document["title"], "Diagnosticar falha de sincronização")
        self.assertEqual(sections[1]["section_key"], "sintoma-pedido-pendente")
        self.assertEqual(sections[1]["metadata"]["source_refs"][0]["source_id"], "artigo-suporte")
        self.assertTrue(self.database.lock_keys[0].startswith("canonical:"))
        for text in [*(row["content"] for row in chunks), *(row["retrieval_text"] for row in chunks), *self.embedding_calls]:
            self.assertNotIn("schema_version:", text)
            self.assertNotIn("source_version:", text)

    def test_yaml_block_scalar_with_indented_delimiter_stays_out_of_body(self):
        text = self.text.replace(
            "notes: URL deliberadamente não resolvível; fixture offline.",
            "notes: |\n      linha inicial\n      ---\n      linha final",
        )
        self.ingest(text)
        for row in self.database.tables["document_chunks"]:
            self.assertNotIn("linha inicial", row["content"])
            self.assertNotIn("linha final", row["retrieval_text"])

    def test_canonical_with_cr_line_endings_keeps_yaml_out_of_body(self):
        self.ingest(self.text.replace("\n", "\r"))
        document = self.database.tables["documents"][0]
        self.assertEqual(document["canonical_id"], "10000000-0000-4000-8000-000000000004")
        for row in self.database.tables["document_chunks"]:
            self.assertNotIn("schema_version:", row["content"])

    def test_noop_rename_upgrade_conflict_and_downgrade(self):
        self.ingest()
        original_calls = len(self.embedding_calls)
        original_id = self.database.tables["documents"][0]["id"]
        reordered = self.text.replace(
            "schema_version: 1.0.0\ndocument_id: 10000000-0000-4000-8000-000000000004",
            "document_id: 10000000-0000-4000-8000-000000000004\nschema_version: 1.0.0",
        )
        self.assertTrue(self.ingest(reordered, force=True)["unchanged"])
        self.assertEqual(len(self.embedding_calls), original_calls)
        renamed = self.ingest(filename="renamed.md")
        self.assertTrue(renamed["reused_embeddings"])
        self.assertEqual(len(self.database.tables["documents"]), 1)
        self.assertEqual(self.database.tables["documents"][0]["filename"], "renamed.md")
        self.assertNotEqual(self.database.tables["documents"][0]["id"], original_id)
        self.assertEqual(len(self.embedding_calls), original_calls)
        higher = self.text.replace("revision: 1", "revision: 2").replace(
            'value: "#pedido-pendente"', 'value: "#novo-localizador"',
        )
        self.ingest(higher, filename="renamed.md")
        self.assertEqual(self.database.tables["documents"][0]["document_revision"], 2)
        self.assertEqual(
            self.database.tables["document_sections"][1]["metadata"]["source_refs"][0]["locator"]["value"],
            "#novo-localizador",
        )
        self.assertEqual(len(self.embedding_calls), original_calls)
        with self.assertRaisesRegex(ValueError, "mesma revisao"):
            self.ingest(higher.replace("Sintoma sintético", "Sintoma revisado"), filename="renamed.md", force=True)
        with self.assertRaisesRegex(ValueError, "anterior"):
            self.ingest(filename="renamed.md", force=True)
        self.assertEqual(self.database.tables["documents"][0]["document_revision"], 2)
        self.assertEqual(len(self.embedding_calls), original_calls)

    def test_incomplete_insert_rolls_back_previous_revision(self):
        self.ingest()
        previous = copy.deepcopy(self.database.tables)
        revised = self.text.replace("revision: 1", "revision: 2").replace("Sintoma sintético", "Sintoma diferente")
        self.database.fail_chunks = True
        result = self.ingest(revised)
        self.assertEqual(result["error"], "falha ao substituir documento")
        self.assertEqual(self.database.tables, previous)

    def test_index_identity_is_checked_before_reusing_embeddings(self):
        self.ingest()
        before = copy.deepcopy(self.database.tables)
        revised = self.text.replace("revision: 1", "revision: 2").replace(
            'value: "#pedido-pendente"', 'value: "#outro-localizador"',
        )
        with patch.object(
            ingest, "ensure_embedding_index_identity", side_effect=ValueError("indice incompatível")
        ) as identity_check:
            result = self.ingest(revised)
        self.assertEqual(result["error"], "falha ao preparar ingestao")
        identity_check.assert_called_with("corpus")
        self.assertEqual(self.database.tables, before)

    def test_invalid_schema_stops_before_embeddings_or_database(self):
        with self.assertRaises(CanonicalDocumentError):
            self.ingest(self.text.replace("revision: 1", "revision: 0"))
        self.assertEqual(self.embedding_calls, [])
        self.assertEqual(self.database.tables["documents"], [])

    def test_missing_section_projection_rejects_canonical_before_embeddings(self):
        with patch.object(ingest, "_document_sections_supported", return_value=False):
            with self.assertRaisesRegex(RuntimeError, "document_sections"):
                self.ingest()
        self.assertEqual(self.embedding_calls, [])
        self.assertEqual(self.database.tables["documents"], [])

    def test_concurrent_revisions_cannot_replace_newer_revision(self):
        self.ingest()
        base = copy.deepcopy(self.database.tables["documents"][0])

        def replace(revision):
            row = copy.deepcopy(base)
            row["id"] = f"revision-{revision}"
            row["filename"] = f"revision-{revision}.md"
            row["document_revision"] = revision
            row["metadata"]["semantic_hash"] = f"hash-{revision}"
            row["processing_hash"] = f"processing-{revision}"
            try:
                return ingest._replace_document_atomically(
                    filename=row["filename"], document_row=row,
                    section_rows=[], chunk_rows=[], force=True,
                )
            except ValueError:
                return False

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(replace, (2, 3)))
        self.assertTrue(any(results))
        self.assertEqual(self.database.tables["documents"][0]["document_revision"], 3)
        self.assertEqual(len(self.database.tables["documents"]), 1)
        self.assertIn("canonical:10000000-0000-4000-8000-000000000004", self.database.lock_keys)

    def test_legacy_file_remains_supported(self):
        result = self.ingest("# Manual legado\n\nCorpo", filename="legacy.md")
        self.assertEqual(result["chunks_count"], 1)
        document = self.database.tables["documents"][0]
        self.assertNotIn("canonical_id", document)
        self.assertEqual(document["filename"], "legacy.md")

    def test_legacy_ingest_cannot_erase_existing_canonical_identity(self):
        self.ingest()
        before = copy.deepcopy(self.database.tables)
        with self.assertRaisesRegex(ValueError, "ingestao legada"):
            self.ingest("# Manual legado\n\nCorpo", filename="a.md", force=True)
        self.assertEqual(self.database.tables, before)


@unittest.skipUnless(
    os.getenv("RUN_DB_INTEGRATION_TESTS") == "1",
    "requer PostgreSQL/pgvector descartável e RUN_DB_INTEGRATION_TESTS=1",
)
class CanonicalIngestPostgresTest(unittest.TestCase):
    def setUp(self):
        db.validate_database_config()
        self.canonical_id = str(uuid4())
        self.filename = f"__canonical_67_{uuid4().hex}.md"
        self.renamed = f"__canonical_67_renamed_{uuid4().hex}.md"
        self.text = FIXTURE.read_text(encoding="utf-8").replace(
            "10000000-0000-4000-8000-000000000004", self.canonical_id,
        )

    def tearDown(self):
        db.db_delete("documents", {"canonical_id": f"eq.{self.canonical_id}"})

    def _ingest(self, text, filename):
        return ingest._ingest_text_source(
            filename=filename, title=Path(filename).stem, source=filename,
            doc_type="md", text=text, source_type="file", force=True,
        )

    def test_rename_conflict_and_failed_replacement_preserve_index(self):
        vector = [0.001] * config.EMBEDDING_DIMENSIONS

        def embed(contents, *_args, **_kwargs):
            return [vector for _ in contents]

        with (
            patch.object(ingest, "_embed_batch_with_retry", side_effect=embed),
            patch.object(ingest, "_save_failed_report_entry"),
            patch.object(config, "CONTEXTUAL_RETRIEVAL_ENABLED", False),
        ):
            self._ingest(self.text, self.filename)
            renamed_result = self._ingest(self.text, self.renamed)
            self.assertTrue(renamed_result["reused_embeddings"])

            def stored():
                rows = db.db_select("documents", filters={"canonical_id": f"eq.{self.canonical_id}"})
                self.assertEqual(len(rows), 1)
                chunks = db.db_select("document_chunks", filters={"document_id": f"eq.{rows[0]['id']}"})
                return rows[0], chunks

            previous, previous_chunks = stored()
            self.assertEqual(previous["filename"], self.renamed)
            self.assertEqual(previous["document_revision"], 1)
            self.assertTrue(previous_chunks)
            sections = db.db_select("document_sections", filters={"document_id": f"eq.{previous['id']}"})
            self.assertIn("sintoma-pedido-pendente", {row["section_key"] for row in sections})

            conflicting = self.text.replace("Sintoma sintético", "Sintoma divergente")
            with self.assertRaisesRegex(ValueError, "mesma revisao"):
                self._ingest(conflicting, self.renamed)

            revised = self.text.replace("revision: 1", "revision: 2").replace(
                "Sintoma sintético", "Sintoma revisado",
            )
            real_insert = ingest.supabase_insert

            def fail_chunks(table, data, *, connection=None):
                if table == "document_chunks":
                    raise RuntimeError("falha simulada")
                return real_insert(table, data, connection=connection)

            with patch.object(ingest, "supabase_insert", side_effect=fail_chunks):
                result = self._ingest(revised, self.renamed)
            self.assertEqual(result["error"], "falha ao substituir documento")
            current, current_chunks = stored()
            self.assertEqual(current["id"], previous["id"])
            self.assertEqual(current_chunks, previous_chunks)


if __name__ == "__main__":
    unittest.main()
