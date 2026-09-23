"""Contrato offline e casos transacionais da promocao canonica."""

import copy
import hashlib
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import yaml

import config
import db
import ingest
import promotion
import rag
from canonical_docs import lint_manifest
from preservation import compute_snapshot_sha256


FIXTURE = Path(__file__).resolve().parents[1] / "contracts/canonical-docs/v1/fixtures/valid/procedure.md"


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class PromotionFixture:
    def __init__(self, root: Path):
        self.root = root
        self.batch = "lote-" + uuid4().hex[:12]
        suffix = uuid4().hex[:8]
        self.canonical_id = str(uuid4())
        self.old_path = f"documentos/old-{suffix}.md"
        self.new_path = f"documentos/new-{suffix}.md"
        (root / "documentos").mkdir(parents=True)
        (root / "contracts/canonical-docs").mkdir(parents=True)
        self.old = root / self.old_path
        self.new = root / self.new_path
        self.old.write_text("# Fonte anterior\n\nCON_USACREDRCA habilita a conta.\n", encoding="utf-8")
        canonical = FIXTURE.read_text(encoding="utf-8").replace(
            "10000000-0000-4000-8000-000000000001", self.canonical_id,
        )
        self.new.write_text(canonical, encoding="utf-8")
        self.before_path = root / "contracts/canonical-docs/manifest.yaml"
        self.after_path = root / "contracts/canonical-docs/candidate.yaml"
        self.before_entry = {
            "path": self.old_path, "document_id": None, "format": "legacy",
            "state": "active", "ingestion": "include", "batch_id": self.batch,
            "exclusion": None, "successors": [],
        }
        self.after_old = {
            **self.before_entry, "state": "superseded", "ingestion": "exclude",
            "exclusion": {"reason": "Promocao revisada", "decided_by": "revisor",
                          "decided_at": "2026-09-23T12:00:00Z", "revisit_after": None},
            "successors": [{"document_id": self.canonical_id,
                            "section_keys": ["habilitar-conta-corrente"]}],
        }
        self.after_new = {
            "path": self.new_path, "document_id": self.canonical_id,
            "format": "canonical", "state": "active", "ingestion": "include",
            "batch_id": self.batch, "exclusion": None, "successors": [],
        }
        self.write_manifests()
        self.report = {
            "schema_version": "1.0.0", "batch_id": self.batch,
            "original_snapshot": {"commit": "a" * 40, "sha256": ""},
            "source_unit_ids": ["old-unit"],
            "units": [{
                "unit_id": "old-unit",
                "source": {"path": self.old_path, "sha256": _digest(self.old.read_bytes()),
                           "start_line": 1, "end_line": 3, "format": "legacy", "revision": None},
                "destination": {"path": self.new_path, "sha256": _digest(self.new.read_bytes()),
                                "canonical_id": self.canonical_id, "section_key": "habilitar-conta-corrente",
                                "locator": {"kind": "line_range", "start": 1, "end": 2}},
                "disposition": None,
            }],
        }
        self.report["original_snapshot"]["sha256"] = compute_snapshot_sha256(self.report)
        self.preservation = {
            "status": "passed", "batch_id": self.batch,
            "original_snapshot": self.report["original_snapshot"],
            "unit_results": [{"unit_id": "old-unit", "status": "passed",
                              "destination": copy.deepcopy(self.report["units"][0]["destination"]),
                              "disposition": None}],
        }
        self.gate = {"status": "passed", "batch_id": self.batch,
                     "checks": [{"id": "source_snapshot_identity", "status": "passed"}]}

    def write_manifests(self):
        basis = {
            "schema_version": "1.0.0", "manifest_id": str(uuid4()), "revision": 1,
            "batches": [{"batch_id": self.batch, "description": "Lote sintetico"}],
        }
        self.before_path.write_text(yaml.safe_dump({**basis, "entries": [self.before_entry]}, allow_unicode=True), encoding="utf-8")
        self.after_path.write_text(yaml.safe_dump({**basis, "revision": 2,
                                                   "entries": [self.after_old, self.after_new]}, allow_unicode=True), encoding="utf-8")

    def plan(self, before: str = "a" * 64, after: str = "b" * 64):
        return promotion.preview_plan(
            root=self.root, before_manifest=self.before_path, after_manifest=self.after_path,
            source_report=self.report, expected_corpus_sha256=before,
            expected_candidate_corpus_sha256=after,
        )


class TestPromotionPreview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fixture = PromotionFixture(Path(self.tmp.name))

    def test_preview_derives_map_and_freezes_both_manifests_and_files(self):
        plan = self.fixture.plan()
        self.assertEqual(plan["predecessors"][0]["successors"][0]["document_id"], self.fixture.canonical_id)
        self.assertEqual(plan["successors"][0]["sha256"], _digest(self.fixture.new.read_bytes()))
        self.assertEqual(plan["before_manifest"]["sha256"], _digest(self.fixture.before_path.read_bytes()))
        self.assertEqual(plan["after_manifest"]["sha256"], _digest(self.fixture.after_path.read_bytes()))
        promotion._verify_plan(plan, self.fixture.root)

    def test_lint_allows_explicitly_retired_file_but_rejects_unlisted_file(self):
        diagnostics, _ = lint_manifest(self.fixture.after_path, self.fixture.root)
        self.assertNotIn("selection-mismatch", {item.rule for item in diagnostics})
        (self.fixture.root / "documentos/unlisted.md").write_text("# Sem entrada", encoding="utf-8")
        diagnostics, _ = lint_manifest(self.fixture.after_path, self.fixture.root)
        self.assertIn("selection-mismatch", {item.rule for item in diagnostics})

    def test_source_or_mapping_drift_fails_preview(self):
        self.fixture.old.write_text("alterado", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "predecessor diverge"):
            self.fixture.plan()
        self.fixture.old.write_text("# Fonte anterior\n\nCON_USACREDRCA habilita a conta.\n", encoding="utf-8")
        self.fixture.report["units"][0]["destination"]["section_key"] = "outro"
        with self.assertRaisesRegex(ValueError, "destino da unidade"):
            self.fixture.plan()

    def test_preview_records_only_line_ending_normalization(self):
        original = self.fixture.old.read_bytes().replace(b"\r\n", b"\n")
        self.fixture.old.write_bytes(original.replace(b"\n", b"\r\n"))
        self.fixture.report["units"][0]["source"]["sha256"] = _digest(original)
        self.fixture.report["original_snapshot"]["sha256"] = compute_snapshot_sha256(self.fixture.report)
        with patch.object(promotion, "_git_source", return_value=original):
            plan = self.fixture.plan()
        self.assertEqual(plan["normalized_source_paths"], [self.fixture.old_path])

    def test_forged_plan_map_fails_rederivation(self):
        plan = self.fixture.plan()
        plan["predecessors"][0]["successors"][0]["section_keys"] = []
        plan["fingerprint"] = _digest(promotion._canonical_json({k: v for k, v in plan.items() if k != "fingerprint"}))
        with self.assertRaisesRegex(ValueError, "mapa do plano"):
            promotion._verify_plan(plan, self.fixture.root)

    def test_gate_requires_both_passed_and_source_snapshot_link(self):
        plan = self.fixture.plan()
        promotion._check_gates(plan, self.fixture.preservation, self.fixture.gate)
        bad_gate = copy.deepcopy(self.fixture.gate)
        bad_gate["checks"] = []
        with self.assertRaisesRegex(ValueError, "gates #69"):
            promotion._check_gates(plan, self.fixture.preservation, bad_gate)

    def test_rename_same_canonical_id_is_included_in_map(self):
        original = self.fixture.new.read_text(encoding="utf-8")
        self.fixture.old.write_text(original, encoding="utf-8")
        self.fixture.before_entry.update({"format": "canonical", "document_id": self.fixture.canonical_id})
        self.fixture.after_old = None
        basis = {
            "schema_version": "1.0.0", "manifest_id": str(uuid4()), "revision": 2,
            "batches": [{"batch_id": self.fixture.batch, "description": "Rename sintetico"}],
        }
        self.fixture.before_path.write_text(yaml.safe_dump({**basis, "entries": [self.fixture.before_entry]}, allow_unicode=True), encoding="utf-8")
        self.fixture.after_path.write_text(yaml.safe_dump({**basis, "revision": 3,
                                                           "entries": [self.fixture.after_new]}, allow_unicode=True), encoding="utf-8")
        self.fixture.report["units"][0]["source"].update({
            "sha256": _digest(self.fixture.old.read_bytes()), "format": "canonical", "revision": 2,
        })
        self.fixture.report["original_snapshot"]["sha256"] = compute_snapshot_sha256(self.fixture.report)
        plan = self.fixture.plan()
        self.assertEqual(plan["predecessors"][0]["canonical_id"], self.fixture.canonical_id)
        self.assertEqual(plan["successors"][0]["canonical_id"], self.fixture.canonical_id)

    def test_same_path_replacement_is_rejected_explicitly(self):
        self.fixture.old.write_bytes(self.fixture.new.read_bytes())
        candidate = {**copy.deepcopy(self.fixture.after_new), "path": self.fixture.old_path}
        self.fixture.after_path.write_text(yaml.safe_dump({
            "schema_version": "1.0.0", "manifest_id": str(uuid4()), "revision": 2,
            "batches": [{"batch_id": self.fixture.batch, "description": "Path reutilizado"}],
            "entries": [candidate],
        }, allow_unicode=True), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "reutilizacao de path ativo"):
            self.fixture.plan()

    def test_preparation_failure_does_not_open_transaction(self):
        plan = self.fixture.plan()
        with patch.object(promotion.db, "db_select", return_value=[]), patch.object(
            promotion, "_prepare_successor", side_effect=RuntimeError("embedding falhou")
        ), patch.object(promotion.db, "db_transaction") as transaction:
            with self.assertRaisesRegex(RuntimeError, "embedding falhou"):
                promotion.apply_plan(plan, self.fixture.preservation, self.fixture.gate, self.fixture.root)
        transaction.assert_not_called()

    def test_prepare_successor_builds_complete_canonical_rows_with_fake_embeddings(self):
        plan = self.fixture.plan()
        calls = []

        def fake_embed(contents, *_args, **_kwargs):
            calls.append(len(contents))
            return [[0.001] * config.EMBEDDING_DIMENSIONS for _ in contents]

        with patch.object(ingest, "_document_sections_supported", return_value=True), patch.object(
            ingest, "ensure_embedding_index_identity"
        ), patch.object(ingest, "_embed_batch_with_retry", side_effect=fake_embed), patch.object(
            config, "CONTEXTUAL_RETRIEVAL_ENABLED", False
        ):
            prepared = promotion._prepare_successor(self.fixture.root, plan["successors"][0])
        self.assertGreater(len(calls), 1)
        self.assertEqual(prepared["document"]["canonical_id"], self.fixture.canonical_id)
        self.assertEqual(prepared["document"]["document_revision"], 2)
        self.assertTrue(prepared["sections"])
        self.assertTrue(prepared["chunks"])
        self.assertEqual(prepared["document"]["chunk_count"], len(prepared["chunks"]))
        self.assertTrue(all(row["embedding"].startswith("[") for row in prepared["sections"] + prepared["chunks"]))
        self.assertTrue(all(row["metadata"]["canonical_id"] == self.fixture.canonical_id
                            for row in prepared["sections"] + prepared["chunks"]))

    def test_split_maps_each_successor(self):
        second_id = str(uuid4())
        second_path = "documentos/second.md"
        (self.fixture.root / second_path).write_text(
            self.fixture.new.read_text(encoding="utf-8").replace(self.fixture.canonical_id, second_id),
            encoding="utf-8",
        )
        self.fixture.after_old["successors"].append({
            "document_id": second_id, "section_keys": ["habilitar-conta-corrente"],
        })
        self.fixture.after_path.write_text(yaml.safe_dump({
            "schema_version": "1.0.0", "manifest_id": str(uuid4()), "revision": 2,
            "batches": [{"batch_id": self.fixture.batch, "description": "Split sintetico"}],
            "entries": [copy.deepcopy(self.fixture.after_old), copy.deepcopy(self.fixture.after_new),
                        {**copy.deepcopy(self.fixture.after_new), "path": second_path, "document_id": second_id}],
        }, allow_unicode=True), encoding="utf-8")
        second_unit = copy.deepcopy(self.fixture.report["units"][0])
        second_unit["unit_id"] = "second-unit"
        second_unit["destination"].update({
            "path": second_path, "sha256": _digest((self.fixture.root / second_path).read_bytes()),
            "canonical_id": second_id,
        })
        self.fixture.report["source_unit_ids"].append("second-unit")
        self.fixture.report["units"].append(second_unit)
        self.fixture.report["original_snapshot"]["sha256"] = compute_snapshot_sha256(self.fixture.report)
        plan = self.fixture.plan()
        self.assertEqual(len(plan["predecessors"]), 1)
        self.assertEqual(len(plan["successors"]), 2)

    def test_merge_maps_two_predecessors_to_one_successor(self):
        second_path = "documentos/second-old.md"
        second_old = self.fixture.root / second_path
        second_old.write_text("# Outra fonte\n\nMais uma regra.\n", encoding="utf-8")
        second_entry = {**copy.deepcopy(self.fixture.before_entry), "path": second_path}
        second_after = {**copy.deepcopy(self.fixture.after_old), "path": second_path}
        self.fixture.before_path.write_text(yaml.safe_dump({
            "schema_version": "1.0.0", "manifest_id": str(uuid4()), "revision": 1,
            "batches": [{"batch_id": self.fixture.batch, "description": "Merge sintetico"}],
            "entries": [copy.deepcopy(self.fixture.before_entry), second_entry],
        }, allow_unicode=True), encoding="utf-8")
        self.fixture.after_path.write_text(yaml.safe_dump({
            "schema_version": "1.0.0", "manifest_id": str(uuid4()), "revision": 2,
            "batches": [{"batch_id": self.fixture.batch, "description": "Merge sintetico"}],
            "entries": [copy.deepcopy(self.fixture.after_old), second_after, copy.deepcopy(self.fixture.after_new)],
        }, allow_unicode=True), encoding="utf-8")
        second_unit = copy.deepcopy(self.fixture.report["units"][0])
        second_unit["unit_id"] = "second-unit"
        second_unit["source"].update({"path": second_path, "sha256": _digest(second_old.read_bytes())})
        self.fixture.report["source_unit_ids"].append("second-unit")
        self.fixture.report["units"].append(second_unit)
        self.fixture.report["original_snapshot"]["sha256"] = compute_snapshot_sha256(self.fixture.report)
        plan = self.fixture.plan()
        self.assertEqual(len(plan["predecessors"]), 2)
        self.assertEqual(len(plan["successors"]), 1)

    def test_normal_ingest_skips_retired_repository_source(self):
        self.fixture.before_path.write_bytes(self.fixture.after_path.read_bytes())
        current_hash = _digest(self.fixture.before_path.read_bytes())
        with patch.object(ingest, "__file__", str(self.fixture.root / "ingest.py")), patch.object(
            ingest, "DEFAULT_MANIFEST", self.fixture.before_path
        ), patch.object(ingest, "db_table_exists", return_value=True), patch.object(
            ingest, "db_select", return_value=[{"status": "applied", "manifest_sha256": current_hash}]
        ), patch.dict(ingest.READERS, {".md": unittest.mock.Mock(side_effect=AssertionError("read"))}):
            result = ingest.ingest_file(str(self.fixture.old))
        self.assertEqual(result["skip_reason"], "manifest_not_active")

    def test_full_context_rejects_pending_manifest(self):
        old_cache = rag._full_context_cache
        rag._full_context_cache = None
        try:
            with patch.multiple(config, FULL_CONTEXT_ENABLED=True, DOCS_DIR=str(self.fixture.root / "documentos")), patch.object(
                rag, "__file__", str(self.fixture.root / "rag.py")
            ), patch.object(rag, "DEFAULT_MANIFEST", self.fixture.before_path), patch.object(
                rag, "get_database_url", return_value="postgresql://fixture"
            ), patch.object(rag, "db_table_exists", return_value=True), patch.object(
                rag, "db_select", return_value=[{"status": "applying_manifest", "manifest_sha256": "0" * 64}]
            ):
                self.assertEqual(rag._load_full_context_docs(), "")
        finally:
            rag._full_context_cache = old_cache

    def test_partial_archive_is_resumable_and_restores_exact_bytes(self):
        original = self.fixture.old.read_bytes()
        snapshot = promotion._file_snapshots(
            self.fixture.root, [self.fixture.old_path], self.fixture.batch, "predecessor",
        )
        promotion._archive_predecessors(self.fixture.root, snapshot)
        self.assertFalse(self.fixture.old.exists())
        promotion._archive_predecessors(self.fixture.root, snapshot)
        promotion._restore_predecessors(self.fixture.root, snapshot)
        self.assertEqual(self.fixture.old.read_bytes(), original)


@unittest.skipUnless(os.getenv("RUN_DB_INTEGRATION_TESTS") == "1", "requer PostgreSQL descartavel")
class TestPromotionPostgres(unittest.TestCase):
    def setUp(self):
        db.validate_database_config()
        self.tmp = tempfile.TemporaryDirectory()
        self.fixture = PromotionFixture(Path(self.tmp.name))
        self.old_id = str(uuid4())
        self.new_id = str(uuid4())
        self.extra_ids = []
        self.embedding = "[" + ",".join(["0.001"] * 1536) + "]"
        with db.db_transaction() as connection:
            db.db_insert("documents", {
                "id": self.old_id, "filename": self.fixture.old.name, "title": "Anterior",
                "source": str(self.fixture.old), "doc_type": "md", "chunk_count": 1,
                "priority": 5, "content_hash": "old", "processing_hash": "old",
            }, connection=connection)
            db.db_insert("document_chunks", {
                "document_id": self.old_id, "content": "conteudo anterior", "chunk_index": 0,
                "embedding": self.embedding, "metadata": {"aliases": ["antigo"]}, "token_count": 2,
            }, connection=connection)
        self.prepared = {
            "document": {
                "id": self.new_id, "filename": self.fixture.new.name, "title": "Novo",
                "source": str(self.fixture.new), "doc_type": "md", "chunk_count": 1,
                "priority": 5, "content_hash": "new", "processing_hash": "new",
                "canonical_id": self.fixture.canonical_id, "schema_version": "1.0.0",
                "document_revision": 2,
                "metadata": {"canonical": {"aliases": ["novo"]}, "semantic_hash": "new"},
            },
            "sections": [],
            "chunks": [{"document_id": self.new_id, "content": "conteudo novo",
                        "chunk_index": 0, "embedding": self.embedding,
                        "metadata": {"aliases": ["novo"]}, "token_count": 2}],
        }
        with db.db_transaction() as connection:
            self.before_hash = promotion._corpus_identity(connection)
        class RollbackProbe(Exception):
            pass
        try:
            with db.db_transaction() as connection:
                db.db_delete("documents", {"id": f"eq.{self.old_id}"}, connection=connection)
                db.db_insert("documents", self.prepared["document"], connection=connection)
                db.db_insert("document_chunks", self.prepared["chunks"], connection=connection)
                self.after_hash = promotion._corpus_identity(connection)
                raise RollbackProbe()
        except RollbackProbe:
            pass
        self.plan = self.fixture.plan(self.before_hash, self.after_hash)

    def tearDown(self):
        with db.db_transaction() as connection:
            for document_id in (self.old_id, self.new_id, *self.extra_ids):
                db.db_delete("documents", {"id": f"eq.{document_id}"}, connection=connection)
            db.db_delete("canonical_publication_state", {"batch_id": f"eq.{self.fixture.batch}"}, connection=connection)
            db.db_delete("canonical_migration_batches", {"batch_id": f"eq.{self.fixture.batch}"}, connection=connection)
        self.tmp.cleanup()

    def _apply(self):
        with patch.object(promotion, "_prepare_successor", return_value=copy.deepcopy(self.prepared)):
            return promotion.apply_plan(self.plan, self.fixture.preservation, self.fixture.gate, self.fixture.root)

    def _candidate_hash(self, *, old_ids, prepared):
        class RollbackProbe(Exception):
            pass
        try:
            with db.db_transaction() as connection:
                for old_id in old_ids:
                    db.db_delete("documents", {"id": f"eq.{old_id}"}, connection=connection)
                for item in prepared:
                    db.db_insert("documents", item["document"], connection=connection)
                    db.db_insert("document_chunks", item["chunks"], connection=connection)
                digest = promotion._corpus_identity(connection)
                raise RollbackProbe(digest)
        except RollbackProbe as exc:
            return str(exc)

    def _add_second_successor(self):
        second_id = str(uuid4())
        second_doc_id = str(uuid4())
        self.extra_ids.append(second_doc_id)
        second_path = f"documentos/second-{uuid4().hex[:8]}.md"
        second_file = self.fixture.root / second_path
        second_file.write_text(self.fixture.new.read_text(encoding="utf-8").replace(
            self.fixture.canonical_id, second_id), encoding="utf-8")
        self.fixture.after_old["successors"].append({"document_id": second_id,
                                                       "section_keys": ["habilitar-conta-corrente"]})
        self.fixture.after_path.write_text(yaml.safe_dump({
            "schema_version": "1.0.0", "manifest_id": str(uuid4()), "revision": 2,
            "batches": [{"batch_id": self.fixture.batch, "description": "Split integrado"}],
            "entries": [copy.deepcopy(self.fixture.after_old), copy.deepcopy(self.fixture.after_new),
                        {**copy.deepcopy(self.fixture.after_new), "path": second_path, "document_id": second_id}],
        }, allow_unicode=True), encoding="utf-8")
        unit = copy.deepcopy(self.fixture.report["units"][0])
        unit["unit_id"] = "second-unit"
        unit["destination"].update({"path": second_path, "canonical_id": second_id,
                                     "sha256": _digest(second_file.read_bytes())})
        self.fixture.report["source_unit_ids"].append("second-unit")
        self.fixture.report["units"].append(unit)
        self.fixture.report["original_snapshot"]["sha256"] = compute_snapshot_sha256(self.fixture.report)
        self.fixture.preservation["original_snapshot"] = self.fixture.report["original_snapshot"]
        self.fixture.preservation["unit_results"].append({
            "unit_id": "second-unit", "status": "passed", "destination": unit["destination"],
            "disposition": None,
        })
        second_prepared = copy.deepcopy(self.prepared)
        second_prepared["document"].update({
            "id": second_doc_id, "filename": second_file.name, "source": str(second_file),
            "canonical_id": second_id,
        })
        second_prepared["chunks"][0]["document_id"] = second_doc_id
        self.after_hash = self._candidate_hash(old_ids=[self.old_id], prepared=[self.prepared, second_prepared])
        self.plan = self.fixture.plan(self.before_hash, self.after_hash)
        return second_prepared

    def _add_second_predecessor(self):
        second_old_id = str(uuid4())
        self.extra_ids.append(second_old_id)
        second_path = f"documentos/old-second-{uuid4().hex[:8]}.md"
        second_file = self.fixture.root / second_path
        second_file.write_text("# Segunda fonte\n\nRegra adicional.\n", encoding="utf-8")
        second_entry = {**copy.deepcopy(self.fixture.before_entry), "path": second_path}
        second_after = {**copy.deepcopy(self.fixture.after_old), "path": second_path}
        self.fixture.before_path.write_text(yaml.safe_dump({
            "schema_version": "1.0.0", "manifest_id": str(uuid4()), "revision": 1,
            "batches": [{"batch_id": self.fixture.batch, "description": "Merge integrado"}],
            "entries": [copy.deepcopy(self.fixture.before_entry), second_entry],
        }, allow_unicode=True), encoding="utf-8")
        self.fixture.after_path.write_text(yaml.safe_dump({
            "schema_version": "1.0.0", "manifest_id": str(uuid4()), "revision": 2,
            "batches": [{"batch_id": self.fixture.batch, "description": "Merge integrado"}],
            "entries": [copy.deepcopy(self.fixture.after_old), second_after, copy.deepcopy(self.fixture.after_new)],
        }, allow_unicode=True), encoding="utf-8")
        unit = copy.deepcopy(self.fixture.report["units"][0])
        unit["unit_id"] = "second-unit"
        unit["source"].update({"path": second_path, "sha256": _digest(second_file.read_bytes())})
        self.fixture.report["source_unit_ids"].append("second-unit")
        self.fixture.report["units"].append(unit)
        self.fixture.report["original_snapshot"]["sha256"] = compute_snapshot_sha256(self.fixture.report)
        self.fixture.preservation["original_snapshot"] = self.fixture.report["original_snapshot"]
        self.fixture.preservation["unit_results"].append({
            "unit_id": "second-unit", "status": "passed", "destination": unit["destination"],
            "disposition": None,
        })
        with db.db_transaction() as connection:
            db.db_insert("documents", {
                "id": second_old_id, "filename": second_file.name, "title": "Anterior 2",
                "source": str(second_file), "doc_type": "md", "chunk_count": 1,
                "priority": 5, "content_hash": "old-2", "processing_hash": "old-2",
            }, connection=connection)
            db.db_insert("document_chunks", {
                "document_id": second_old_id, "content": "conteudo anterior 2", "chunk_index": 0,
                "embedding": self.embedding, "metadata": {}, "token_count": 3,
            }, connection=connection)
            self.before_hash = promotion._corpus_identity(connection)
        self.after_hash = self._candidate_hash(old_ids=[self.old_id, second_old_id], prepared=[self.prepared])
        self.plan = self.fixture.plan(self.before_hash, self.after_hash)
        return second_old_id

    def test_apply_idempotent_and_rollback_restores_vector_and_alias(self):
        self.assertEqual(self._apply()["status"], "applied")
        self.assertEqual(self._apply()["status"], "already_applied")
        self.assertEqual(db.db_select("documents", filters={"id": f"eq.{self.old_id}"}), [])
        self.assertEqual(promotion.rollback_plan(self.plan, self.fixture.root)["status"], "rolled_back")
        self.assertEqual(promotion.rollback_plan(self.plan, self.fixture.root)["status"], "already_rolled_back")
        restored = db.db_select("document_chunks", filters={"document_id": f"eq.{self.old_id}"})
        self.assertEqual(restored[0]["metadata"]["aliases"], ["antigo"])
        self.assertEqual(str(restored[0]["embedding"]), self.embedding)

    def test_failure_on_last_successor_rolls_back_everything(self):
        second = self._add_second_successor()
        real_insert = promotion.ingest._insert_chunk_rows
        calls = 0

        def fail_last(rows, *, connection):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("ultimo sucessor")
            return real_insert(rows, connection=connection)

        with patch.object(promotion, "_prepare_successor", side_effect=[copy.deepcopy(self.prepared), second]), patch.object(
            promotion.ingest, "_insert_chunk_rows", side_effect=fail_last
        ):
            with self.assertRaisesRegex(RuntimeError, "ultimo sucessor"):
                promotion.apply_plan(self.plan, self.fixture.preservation, self.fixture.gate, self.fixture.root)
        self.assertEqual(len(db.db_select("documents", filters={"id": f"eq.{self.old_id}"})), 1)
        self.assertEqual(db.db_select("documents", filters={"id": f"eq.{self.new_id}"}), [])
        self.assertEqual(db.db_select("documents", filters={"id": f"eq.{second['document']['id']}"}), [])
        self.assertEqual(db.db_select("canonical_migration_batches", filters={"batch_id": f"eq.{self.fixture.batch}"}), [])

    def test_split_and_merge_restore_all_predecessors(self):
        second_old_id = self._add_second_predecessor()
        self.assertEqual(self._apply()["status"], "applied")
        self.assertEqual(db.db_select("documents", filters={"id": f"eq.{second_old_id}"}), [])
        self.assertEqual(promotion.rollback_plan(self.plan, self.fixture.root)["status"], "rolled_back")
        self.assertEqual(len(db.db_select("documents", filters={"id": f"eq.{self.old_id}"})), 1)
        self.assertEqual(len(db.db_select("documents", filters={"id": f"eq.{second_old_id}"})), 1)

    def test_concurrent_apply_is_serialized(self):
        barrier = Barrier(2)

        def prepare(*_args):
            barrier.wait(timeout=15)
            return copy.deepcopy(self.prepared)

        def run():
            try:
                return promotion.apply_plan(self.plan, self.fixture.preservation,
                                            self.fixture.gate, self.fixture.root)["status"]
            except ValueError:
                return "conflict"

        with patch.object(promotion, "_prepare_successor", side_effect=prepare), ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda _index: run(), range(2)))
        self.assertIn("applied", statuses)
        self.assertIn(statuses[1] if statuses[0] == "applied" else statuses[0], {"conflict", "already_applied"})
        self.assertEqual(self._apply()["status"], "already_applied")

    def test_snapshot_conflict_blocks_apply(self):
        bad = copy.deepcopy(self.plan)
        bad["expected_corpus_sha256"] = "0" * 64
        bad["fingerprint"] = _digest(promotion._canonical_json({k: v for k, v in bad.items() if k != "fingerprint"}))
        with patch.object(promotion, "_prepare_successor", return_value=copy.deepcopy(self.prepared)):
            with self.assertRaisesRegex(ValueError, "snapshot de corpus"):
                promotion.apply_plan(bad, self.fixture.preservation, self.fixture.gate, self.fixture.root)
        self.assertEqual(len(db.db_select("documents", filters={"id": f"eq.{self.old_id}"})), 1)

    def test_publication_failure_can_rollback_without_reembedding(self):
        with patch.object(promotion, "_prepare_successor", return_value=copy.deepcopy(self.prepared)), patch.object(
            promotion, "_publish_manifest", side_effect=OSError("filesystem")
        ):
            with self.assertRaises(promotion.PublicationPending):
                promotion.apply_plan(self.plan, self.fixture.preservation, self.fixture.gate, self.fixture.root)
        ledger = db.db_select("canonical_migration_batches", filters={"batch_id": f"eq.{self.fixture.batch}"})[0]
        self.assertEqual(ledger["status"], "applying_manifest")
        self.assertEqual(promotion.rollback_plan(self.plan, self.fixture.root)["status"], "rolled_back")

    def test_rename_partial_archive_resume_and_exact_rollback(self):
        original = self.fixture.new.read_bytes()
        self.fixture.old.write_bytes(original)
        self.fixture.before_entry.update({"format": "canonical", "document_id": self.fixture.canonical_id})
        basis = {
            "schema_version": "1.0.0", "manifest_id": str(uuid4()), "revision": 2,
            "batches": [{"batch_id": self.fixture.batch, "description": "Rename integrado"}],
        }
        self.fixture.before_path.write_text(yaml.safe_dump({**basis, "entries": [self.fixture.before_entry]},
                                                       allow_unicode=True), encoding="utf-8")
        self.fixture.after_path.write_text(yaml.safe_dump({**basis, "revision": 3,
                                                         "entries": [self.fixture.after_new]},
                                                       allow_unicode=True), encoding="utf-8")
        self.fixture.report["units"][0]["source"].update({
            "sha256": _digest(original), "format": "canonical", "revision": 2,
        })
        self.fixture.report["original_snapshot"]["sha256"] = compute_snapshot_sha256(self.fixture.report)
        self.fixture.preservation["original_snapshot"] = self.fixture.report["original_snapshot"]
        with db.db_transaction() as connection:
            db.db_update("documents", {
                "canonical_id": self.fixture.canonical_id, "schema_version": "1.0.0",
                "document_revision": 2, "metadata": {"canonical": {"aliases": ["original"]}},
            }, {"id": f"eq.{self.old_id}"}, connection=connection)
            self.before_hash = promotion._corpus_identity(connection)
        self.after_hash = self._candidate_hash(old_ids=[self.old_id], prepared=[self.prepared])
        self.plan = self.fixture.plan(self.before_hash, self.after_hash)
        with patch.object(promotion, "_prepare_successor", return_value=copy.deepcopy(self.prepared)), patch.object(
            promotion, "_publish_manifest", side_effect=OSError("falha simulada no YAML")
        ):
            with self.assertRaises(promotion.PublicationPending):
                promotion.apply_plan(self.plan, self.fixture.preservation, self.fixture.gate, self.fixture.root)
        self.assertFalse(self.fixture.old.exists())
        self.assertEqual(self._apply()["status"], "applied")
        diagnostics, _ = lint_manifest(self.fixture.before_path, self.fixture.root)
        self.assertNotIn("selection-mismatch", {item.rule for item in diagnostics})
        self.assertEqual(promotion.rollback_plan(self.plan, self.fixture.root)["status"], "rolled_back")
        self.assertEqual(self.fixture.old.read_bytes(), original)
        self.assertFalse(self.fixture.new.exists())
        diagnostics, _ = lint_manifest(self.fixture.before_path, self.fixture.root)
        self.assertNotIn("selection-mismatch", {item.rule for item in diagnostics})
