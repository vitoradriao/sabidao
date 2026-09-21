import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import canonical_docs
import config
import ingest
from markdown_parser import parse_markdown, split_markdown_sections


ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "contracts" / "canonical-docs" / "manifest.yaml"
VALID_FIXTURES = ROOT / "contracts" / "canonical-docs" / "v1" / "fixtures" / "valid"


class MarkdownParserTest(unittest.TestCase):
    def test_fences_h5_h6_repeated_headings_tables_and_lists(self):
        text = (
            "# Manual\n\n"
            "```sh\n# comentário, não seção\n```\n\n"
            "##### Detalhe\n\n- item\n\n"
            "| Campo | Valor |\n| --- | --- |\n| A | B |\n\n"
            "###### Mesmo nome\n\n###### Mesmo nome\n"
        )

        parsed = parse_markdown(text)
        sections = split_markdown_sections(text)

        self.assertEqual(
            [(heading.level, heading.title) for heading in parsed.headings],
            [(1, "Manual"), (5, "Detalhe"), (6, "Mesmo nome"), (6, "Mesmo nome")],
        )
        self.assertEqual(len(sections), 4)
        self.assertIn("list", {block.kind for block in parsed.blocks})
        self.assertIn("table", {block.kind for block in parsed.blocks})

    def test_catalog_without_headings_and_unclosed_fence_are_explicit(self):
        catalog = "001 - primeiro\n002 - segundo\n"
        self.assertEqual(split_markdown_sections(catalog), [(None, catalog)])

        parsed = parse_markdown("# Manual\n```sql\nselect 1\n")
        self.assertEqual(parsed.unclosed_fence_line, 2)

    def test_real_legacy_edge_cases_match_the_inventory(self):
        sql = parse_markdown(
            (ROOT / "documentos" / "05-SQL-BANCO-E-INTEGRACAO.md").read_text(
                encoding="utf-8"
            )
        )
        scripts = parse_markdown(
            (ROOT / "documentos" / "08-SCRIPTS-UTILITARIOS.md").read_text(
                encoding="utf-8"
            )
        )
        groups = parse_markdown(
            (ROOT / "documentos" / "07-GRUPOS-TABELAS-CARGA-TOTAL.md").read_text(
                encoding="utf-8"
            )
        )
        campaign = parse_markdown(
            (ROOT / "documentos" / "23-CAMPANHA-PROGRESSIVA.md").read_text(
                encoding="utf-8"
            )
        )

        sql_titles = {heading.title for heading in sql.headings}
        script_titles = {heading.title for heading in scripts.headings}
        self.assertNotIn("Instalar TZ-Data no Alpine", sql_titles)
        self.assertNotIn("ENV = TZ = TIMEZONE", sql_titles)
        self.assertNotIn("Verificar snaps instalados", script_titles)
        self.assertNotIn("Remover snaps", script_titles)
        self.assertEqual(groups.headings, ())
        self.assertTrue(any(heading.level == 5 for heading in campaign.headings))


class CanonicalDocsTest(unittest.TestCase):
    def test_manifest_preserves_27_legacy_sources_and_identifies_backups(self):
        diagnostics, manifest = canonical_docs.lint_manifest(MANIFEST, ROOT)
        inventory = canonical_docs.build_inventory(manifest, ROOT)

        self.assertFalse([item for item in diagnostics if item.severity == "error"])
        self.assertEqual(inventory["manifest_entries"], 27)
        self.assertEqual(inventory["included"], 27)
        self.assertEqual(inventory["legacy"], 27)
        self.assertEqual(inventory["canonical"], 0)
        self.assertEqual(len(inventory["backup_files"]), 10)
        self.assertFalse(inventory["business_rules_in_corpus"])

    def test_valid_canonical_document_exposes_stable_section_keys(self):
        document, parsed = canonical_docs.validate_canonical_document(
            VALID_FIXTURES / "procedure.md"
        )
        parsed_keys = {
            heading.section_key
            for heading in parsed.headings
            if heading.section_key is not None
        }
        self.assertEqual(parsed_keys, set(document["sections"]))

    def test_invalid_canonical_document_stops_before_external_calls(self):
        valid_text = (VALID_FIXTURES / "procedure.md").read_text(encoding="utf-8")
        invalid_text = valid_text.replace("schema_version: 1.0.0", "schema_version: 9.0.0")

        with (
            patch.object(ingest, "_infer_module") as infer_module,
            patch.object(ingest, "_embed_batch_with_retry") as embed,
            self.assertRaises(canonical_docs.CanonicalDocumentError),
        ):
            ingest._ingest_text_source(
                filename="invalid.md",
                title="Inválido",
                source="/docs/invalid.md",
                doc_type="md",
                text=invalid_text,
                source_type="file",
                force=False,
            )

        infer_module.assert_not_called()
        embed.assert_not_called()

    def test_lint_cli_returns_nonzero_and_structured_location_for_error(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp_dir:
            temp_path = Path(temp_dir)
            relative_dir = temp_path.relative_to(ROOT).as_posix()
            document = temp_path / "invalid.md"
            valid_text = (VALID_FIXTURES / "procedure.md").read_text(encoding="utf-8")
            document.write_text(
                valid_text.replace("schema_version: 1.0.0", "schema_version: 9.0.0"),
                encoding="utf-8",
            )
            manifest = temp_path / "manifest.yaml"
            manifest.write_text(
                "\n".join(
                    [
                        "schema_version: 1.0.0",
                        "manifest_id: 30000000-0000-4000-8000-000000000099",
                        "revision: 1",
                        "batches:",
                        "  - batch_id: teste",
                        "    description: Teste sintético.",
                        "entries:",
                        f"  - path: {relative_dir}/invalid.md",
                        "    document_id: 10000000-0000-4000-8000-000000000001",
                        "    format: canonical",
                        "    state: active",
                        "    ingestion: include",
                        "    batch_id: teste",
                        "    exclusion: null",
                        "    successors: []",
                    ]
                ) + "\n",
                encoding="utf-8",
            )
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = canonical_docs.main(
                    ["lint", "--manifest", str(manifest), "--repo-root", str(ROOT)]
                )

        self.assertEqual(exit_code, 1)
        self.assertIn("schema-invalid", output.getvalue())
        self.assertIn("[schema_version]", output.getvalue())

    def test_legacy_ingest_uses_real_headings(self):
        text = (
            "# Manual\n\n"
            "```sh\n# comentário\n```\n\n"
            "##### Operação\nconteúdo\n"
        )
        with patch.object(config, "ANALYTICAL_CONTEXT_ENABLED", False):
            sections = ingest._split_markdown_sections(
                text,
                doc_title="Manual",
                base_module="geral",
                doc_type="md",
            )

        self.assertEqual([section.title for section in sections], ["Manual", "Operação"])


if __name__ == "__main__":
    unittest.main()
