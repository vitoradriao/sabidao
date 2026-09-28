import unittest
from dataclasses import replace
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import ingest
import config
from canonical_docs import CanonicalDocumentError
from scripts.taxonomy_dry_run import compare, main
from taxonomy import RULE_VERSION, editorial, heuristic


FIXTURE = Path(__file__).resolve().parents[1] / "contracts/canonical-docs/v1/fixtures/valid/troubleshooting.md"


class TaxonomyTests(unittest.TestCase):
    def test_competing_signals_have_stable_precedence(self):
        cases = [
            ("Conta Corrente", "Tabela MXSPARAMETRO", "financeiro_pagamentos", "configuracao", "configuration"),
            ("Pedido com erro", "SELECT * FROM MXSINTEGRACAO", "sql_integracao", "sql_integracao", "troubleshooting"),
            ("Fluxo de integração", "Enviar dados para ERP", "sql_integracao", "sql_integracao", "procedure"),
            ("Permissão de usuário", "Habilitar acesso", "geral", "configuracao", "configuration"),
            ("Consulta SQL de parâmetros", "SELECT * FROM MXSPARAMETRO", "sql_integracao", "sql_integracao", "sql_lookup"),
            ("Parâmetros da integração", "Tabela MXSPARAMETRO", "sql_integracao", "configuracao", "configuration"),
            ("05-PEDIDOS-E-VENDAS", "", "geral", "pedidos_vendas", "unknown"),
            ("06-CAMPANHAS-E-DESCONTOS", "", "geral", "pedidos_vendas", "unknown"),
        ]
        for title, content, legacy, module, mode in cases:
            with self.subTest(title=title):
                result = heuristic(
                    title=title, content=content, legacy_module=legacy,
                    legacy_answer_mode="general",
                )
                self.assertEqual((result.primary_module, result.answer_mode), (module, mode))
                self.assertEqual(result.metadata()["taxonomy_version"], RULE_VERSION)
                self.assertTrue(result.signals)
                if title == "Conta Corrente":
                    self.assertEqual(result.secondary_modules, ("financeiro",))

    def test_override_beats_inheritance_and_heuristic(self):
        text = FIXTURE.read_text(encoding="utf-8")
        changed = text.replace(
            "classification_override: {answer_mode: troubleshooting}",
            "classification_override: {primary_module: configuracao, secondary_modules: [financeiro], answer_mode: configuration}",
        )
        document, sections = ingest._classify_text_sections(
            changed, filename="renamed.md", title="Outro nome", source="renamed.md", doc_type="md"
        )
        target = next(section for section in sections if "Pedido permanece" in section.title)
        self.assertEqual(document.primary_module, "pedidos_vendas")
        self.assertEqual(target.classification.primary_module, "configuracao")
        self.assertEqual(target.classification.reason, "editorial_override")
        self.assertEqual(target.module, "parametros_configuracao")
        self.assertEqual(target.answer_mode, "configuration")
        self.assertEqual(target.classification.secondary_modules, ("financeiro",))

    def test_explicit_classification_survives_filename_and_heading_rename(self):
        original = FIXTURE.read_text(encoding="utf-8")
        renamed = original.replace("Pedido permanece pendente", "Pedido continua pendente")
        first, first_sections = ingest._classify_text_sections(
            original, filename="original.md", title="Original", source="original.md", doc_type="md"
        )
        second, second_sections = ingest._classify_text_sections(
            renamed, filename="renamed.md", title="Renamed", source="renamed.md", doc_type="md"
        )
        self.assertEqual(first.metadata(), second.metadata())
        self.assertEqual(first_sections[-1].classification.metadata(),
                         second_sections[-1].classification.metadata())

    def test_invalid_editorial_values_are_rejected(self):
        with self.assertRaises(ValueError):
            editorial({"taxonomy_version": RULE_VERSION, "primary_module": "inventado",
                       "secondary_modules": [], "answer_mode": "reference"})
        with self.assertRaises(ValueError):
            editorial({"taxonomy_version": "outra", "primary_module": "configuracao",
                       "secondary_modules": [], "answer_mode": "reference"})
        invalid_text = FIXTURE.read_text(encoding="utf-8").replace(
            "primary_module: pedidos_vendas", "primary_module: inventado"
        )
        with self.assertRaises(CanonicalDocumentError):
            ingest._classify_text_sections(
                invalid_text, filename="bad.md", title="Bad", source="bad.md", doc_type="md"
            )

    def test_canonical_bom_is_rejected_before_legacy_classification(self):
        with self.assertRaises(CanonicalDocumentError):
            ingest._classify_text_sections(
                "\ufeff" + FIXTURE.read_text(encoding="utf-8"),
                filename="bad.md", title="Bad", source="bad.md", doc_type="md",
            )

    def test_parameter_ficha_corrects_legacy_answer_mode(self):
        _document, sections = ingest._classify_text_sections(
            "# Conta Corrente\n\nTabela MXSPARAMETRO.",
            filename="conta.md", title="Conta Corrente", source="conta.md", doc_type="md",
        )
        self.assertEqual(sections[0].classification.answer_mode, "configuration")
        self.assertEqual(sections[0].answer_mode, "configuration")

    def test_clone_preserves_section_module_and_classification(self):
        source_section = {"id": "section-a", "section_index": 0, "module": "sql_integracao",
                          "metadata": {"classification": {"primary_module": "sql_integracao"}}}
        source_chunk = {"chunk_index": 0, "module": "sql_integracao", "metadata": {
            "module": "sql_integracao", "classification": {"primary_module": "sql_integracao"}},
            "section_id": "section-a"}
        with patch.object(ingest, "_document_sections_supported", return_value=True), patch.object(
            ingest, "supabase_select", side_effect=[[source_section], [source_chunk]]
        ):
            sections, chunks = ingest._clone_prepared_rows(
                source_document_id="old", document_id="new", filename="copy.md", title="Copy",
                doc_type="md", source_type="file", module="pedidos_vendas", doc_priority=10,
                content_hash="same", processing_hash="same",
            )
        self.assertEqual(sections[0]["module"], "sql_integracao")
        self.assertEqual(chunks[0]["module"], "sql_integracao")
        self.assertEqual(chunks[0]["metadata"]["module"], "sql_integracao")
        self.assertEqual(chunks[0]["metadata"]["classification"]["primary_module"], "sql_integracao")
        self.assertEqual(chunks[0]["section_id"], sections[0]["id"])

    def test_offline_report_has_before_after_without_writes(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "conta-corrente.md"
            path.write_text("# Conta Corrente\n\nTabela MXSPARAMETRO.", encoding="utf-8")
            with patch.object(ingest, "supabase_select") as database:
                report = compare(path)
            database.assert_not_called()
        self.assertEqual(report["after"]["classification"]["primary_module"], "configuracao")
        self.assertEqual(report["after"]["module"], "parametros_configuracao")
        self.assertIn("before", report)

    def test_dry_run_counts_all_eligible_files_and_limits_only_examples(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("conta-a.md", "conta-b.md"):
                (root / name).write_text("# Conta Corrente\n\nTabela MXSPARAMETRO.", encoding="utf-8")
            backup = root / "docbkp"
            backup.mkdir()
            (backup / "old.md").write_text("# Antigo", encoding="utf-8")
            output = StringIO()
            with patch.object(ingest, "supabase_select") as database, redirect_stdout(output):
                self.assertEqual(main(["--root", str(root), "--sample-size", "1"]), 0)
            database.assert_not_called()
        report = json.loads(output.getvalue())
        self.assertEqual(report["documents_count"], 2)
        self.assertEqual(len(report["documents"]), 2)
        self.assertLessEqual(len(report["section_changes_sample"]), 1)
        self.assertIn("before_sections", report["documents"][0])
        self.assertIn("after_sections", report["documents"][0])
        self.assertEqual(report["database_writes"], 0)

    def test_classification_metadata_and_processing_hash_include_secondary_module(self):
        classification, sections = ingest._classify_text_sections(
            "# Conta Corrente\n\nTabela MXSPARAMETRO.", filename="conta.md",
            title="Conta Corrente", source="conta.md", doc_type="md"
        )
        section = sections[0]
        self.assertEqual(section.classification.secondary_modules, ("financeiro",))
        row = ingest._build_chunk_row(
            doc_id="doc", chunk_index=0, clean_content=section.content,
            retrieval_text=section.content, contextualization_version=None,
            filename="conta.md", doc_type="md", source_type="file", module=classification.legacy_module,
            title="Conta Corrente", doc_priority=10, content_hash="content", processing_hash="processing",
            embedding=[0.1] * config.EMBEDDING_DIMENSIONS, section=section,
        )
        self.assertEqual(row["metadata"]["classification"]["secondary_modules"], ["financeiro"])
        options = dict(content_hash="content", title="Conta Corrente", doc_type="md",
                       module=classification.legacy_module, doc_priority=10, sections=sections,
                       chunk_items=[(0, section.content, section.content, section)],
                       document_classification=classification)
        with patch.object(ingest, "_document_sections_supported", return_value=False), patch.object(
            ingest.config, "CONTEXTUAL_RETRIEVAL_ENABLED", False
        ):
            first = ingest._processing_hash(**options)
            section.classification = replace(section.classification, secondary_modules=())
            second = ingest._processing_hash(**options)
        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
