import json
import logging
import unittest
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

import config
import db
import ingest


SENSITIVE_FILENAME = "clientes/acme/manual-com-token.md"
SENSITIVE_URL = "https://intranet.invalid/manual?token=nao-compartilhar"
SENSITIVE_CONTENT = "conteudo-confidencial-do-cliente"
SENSITIVE_ERROR = "senha-super-secreta Failing row contains (_SENSITIVE)"


class TestIngestLogSanitization(unittest.TestCase):
    def assert_no_sensitive_data(self, logs: str) -> None:
        for marker in (
            SENSITIVE_FILENAME,
            SENSITIVE_URL,
            SENSITIVE_CONTENT,
            SENSITIVE_ERROR,
            "manual-com-token.md",
            "intranet.invalid",
            "nao-compartilhar",
        ):
            self.assertNotIn(marker, logs)

    def test_file_reader_failure_uses_opaque_source_and_generic_result(self):
        reader = Mock(
            side_effect=RuntimeError(
                f"{SENSITIVE_ERROR} {SENSITIVE_URL} {SENSITIVE_CONTENT}"
            )
        )
        with patch.dict(ingest.READERS, {".md": reader}), self.assertLogs(
            ingest.logger, level=logging.ERROR
        ) as captured:
            result = ingest.ingest_file(
                r"C:\clientes\acme\manual-com-token.md",
                filename_override=SENSITIVE_FILENAME,
            )

        logs = "\n".join(captured.output)
        self.assertIn(f"source_id={ingest._source_id(SENSITIVE_FILENAME)}", logs)
        self.assertIn("stage=read", logs)
        self.assertIn("error_type=RuntimeError", logs)
        self.assert_no_sensitive_data(logs)
        self.assertEqual(result["error"], "falha ao ler arquivo")
        self.assertEqual(result["error_type"], "RuntimeError")
        self.assertNotIn(SENSITIVE_ERROR, str(result))

    def test_pdf_library_warnings_are_suppressed(self):
        pdf_logger = logging.getLogger("PyPDF2._reader")
        with self.assertNoLogs(level=logging.WARNING):
            pdf_logger.warning(SENSITIVE_CONTENT)

    def test_url_file_open_failure_is_sanitized(self):
        with (
            patch.object(Path, "exists", return_value=True),
            patch.object(
                Path,
                "open",
                side_effect=PermissionError(
                    f"{SENSITIVE_ERROR} {SENSITIVE_FILENAME} {SENSITIVE_URL}"
                ),
            ),
            self.assertLogs(ingest.logger, level=logging.WARNING) as captured,
        ):
            urls = ingest._load_urls_from_file(SENSITIVE_FILENAME)

        logs = "\n".join(captured.output)
        self.assertEqual(urls, [])
        self.assertIn(
            f"source_id={ingest._source_id(Path(SENSITIVE_FILENAME))}",
            logs,
        )
        self.assertIn("stage=load_url_file", logs)
        self.assertIn("error_type=PermissionError", logs)
        self.assert_no_sensitive_data(logs)

    def test_parser_failure_logs_only_stage_and_error_type(self):
        with (
            patch.object(
                ingest,
                "_split_markdown_sections",
                side_effect=RuntimeError(
                    f"{SENSITIVE_ERROR} {SENSITIVE_URL} {SENSITIVE_CONTENT}"
                ),
            ),
            self.assertLogs(ingest.logger, level=logging.ERROR) as captured,
            self.assertRaises(RuntimeError),
        ):
            ingest._ingest_text_source(
                filename=SENSITIVE_FILENAME,
                title="Manual confidencial",
                source=SENSITIVE_URL,
                doc_type="md",
                text=f"# Manual\n\n{SENSITIVE_CONTENT}",
                source_type="url",
                force=True,
            )

        logs = "\n".join(captured.output)
        self.assertIn("stage=parse", logs)
        self.assertIn("error_type=RuntimeError", logs)
        self.assert_no_sensitive_data(logs)

    def test_contextual_provider_body_and_error_are_not_logged(self):
        cases = (
            SimpleNamespace(
                text=f"{SENSITIVE_ERROR} {SENSITIVE_URL} {SENSITIVE_CONTENT}"
            ),
            RuntimeError(
                f"{SENSITIVE_ERROR} {SENSITIVE_URL} {SENSITIVE_CONTENT}"
            ),
        )
        for provider_result in cases:
            with self.subTest(provider_result=type(provider_result).__name__):
                provider = (
                    Mock(side_effect=provider_result)
                    if isinstance(provider_result, BaseException)
                    else Mock(return_value=provider_result)
                )
                with (
                    patch.object(config, "CONTEXTUAL_RETRIEVAL_ENABLED", True),
                    patch.object(ingest, "_wait_for_contextual_slot"),
                    patch("rag._gemini_generate", provider),
                    self.assertLogs(ingest.logger, level=logging.WARNING) as captured,
                ):
                    result = ingest._contextualize_chunks_batch(
                        chunks_with_indices=[(0, SENSITIVE_CONTENT)],
                        full_document=SENSITIVE_CONTENT,
                        filename=SENSITIVE_FILENAME,
                        source_id="opaque-source",
                    )

                logs = "\n".join(captured.output)
                self.assertEqual(result, [(0, SENSITIVE_CONTENT)])
                self.assertIn("source_id=opaque-source", logs)
                self.assertIn("stage=contextualize", logs)
                self.assert_no_sensitive_data(logs)

    def test_embedding_fallback_omits_document_and_exception_text(self):
        section = ingest.AnalyticalSection(
            section_index=0,
            title="Secao confidencial",
            heading_path="Manual > Secao",
            content=SENSITIVE_CONTENT,
            module="geral",
            answer_mode="general",
            entities={},
            semantic_context="",
        )
        with (
            patch.object(config, "CONTEXTUAL_RETRIEVAL_ENABLED", False),
            patch.object(ingest, "ensure_embedding_index_identity"),
            patch.object(
                ingest,
                "_embed_batch_with_retry",
                side_effect=RuntimeError(
                    f"{SENSITIVE_ERROR} {SENSITIVE_URL} {SENSITIVE_CONTENT}"
                ),
            ),
            self.assertLogs(ingest.logger, level=logging.ERROR) as captured,
        ):
            rows, failed_chunks = ingest._prepare_chunk_rows(
                doc_id="documento-1",
                filename=SENSITIVE_FILENAME,
                title="Manual confidencial",
                text=SENSITIVE_CONTENT,
                doc_type="md",
                source_type="file",
                module="geral",
                doc_priority=5,
                chunk_items=[(0, SENSITIVE_CONTENT, SENSITIVE_CONTENT, section)],
                source_id="opaque-source",
            )

        logs = "\n".join(captured.output)
        self.assertEqual(rows, [])
        self.assertEqual(failed_chunks, [0])
        self.assertIn("source_id=opaque-source", logs)
        self.assertIn("stage=embedding_batch", logs)
        self.assertIn("stage=embedding_chunk", logs)
        self.assert_no_sensitive_data(logs)

    def test_document_sections_probe_omits_database_error(self):
        ingest._document_sections_available = None
        try:
            with (
                patch.object(
                    ingest,
                    "supabase_select",
                    side_effect=RuntimeError(f"{SENSITIVE_ERROR} {SENSITIVE_URL}"),
                ),
                self.assertLogs(ingest.logger, level=logging.WARNING) as captured,
            ):
                self.assertFalse(ingest._document_sections_supported())
        finally:
            ingest._document_sections_available = None

        logs = "\n".join(captured.output)
        self.assertIn("stage=check_document_sections", logs)
        self.assertIn("error_type=RuntimeError", logs)
        self.assert_no_sensitive_data(logs)

    def test_persistence_failure_keeps_report_but_sanitizes_all_logs(self):
        vector = [0.001] * config.EMBEDDING_DIMENSIONS
        with (
            patch.object(ingest, "supabase_select", return_value=[]),
            patch.object(ingest, "_document_sections_supported", return_value=False),
            patch.object(ingest, "ensure_embedding_index_identity"),
            patch.object(ingest, "_embed_batch_with_retry", return_value=[vector]),
            patch.object(
                ingest,
                "_replace_document_atomically",
                side_effect=RuntimeError(
                    f"{SENSITIVE_ERROR} {SENSITIVE_URL} {SENSITIVE_CONTENT}"
                ),
            ),
            patch.object(ingest, "_save_failed_report_entry") as save_report,
            patch.object(config, "CONTEXTUAL_RETRIEVAL_ENABLED", False),
            self.assertLogs(ingest.logger, level=logging.INFO) as captured,
        ):
            result = ingest._ingest_text_source(
                filename=SENSITIVE_FILENAME,
                title="Manual confidencial",
                source=SENSITIVE_URL,
                doc_type="md",
                text=f"# Manual\n\n{SENSITIVE_CONTENT}",
                source_type="url",
                force=True,
            )

        logs = "\n".join(captured.output)
        self.assertEqual(result["error"], "falha ao substituir documento")
        self.assertIn("stage=persist", logs)
        self.assertIn("error_type=RuntimeError", logs)
        self.assert_no_sensitive_data(logs)
        save_report.assert_called_once()

    def test_directory_boundary_logs_only_opaque_source(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            path = root / "manual-com-token.md"
            path.write_text("conteudo", encoding="utf-8")
            with (
                patch.object(
                    ingest,
                    "ingest_file",
                    side_effect=RuntimeError(
                        f"{SENSITIVE_ERROR} {SENSITIVE_URL} {SENSITIVE_CONTENT}"
                    ),
                ),
                self.assertLogs(ingest.logger, level=logging.ERROR) as captured,
            ):
                result = ingest.ingest_directory(
                    directory=str(root),
                    recursive=False,
                )

        logs = "\n".join(captured.output)
        self.assertEqual(result, [])
        self.assertIn(f"source_id={ingest._source_id(path.name)}", logs)
        self.assertIn("stage=ingest", logs)
        self.assert_no_sensitive_data(logs)

    def test_chunk_logs_never_include_document_content(self):
        with self.assertLogs(ingest.logger, level=logging.INFO) as captured:
            ingest.chunk_text(SENSITIVE_CONTENT)
            ingest.chunk_text_with_context(
                SENSITIVE_CONTENT,
                doc_title=SENSITIVE_FILENAME,
            )

        self.assert_no_sensitive_data("\n".join(captured.output))

    def test_web_source_uses_one_identifier_across_pipeline(self):
        generated_filename = ingest._url_to_filename(SENSITIVE_URL)
        self.assertEqual(
            ingest._url_source_id(SENSITIVE_URL),
            ingest._source_id(generated_filename),
        )

    def test_failure_report_preserves_provenance_for_retry(self):
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_path = root / "manual.md"
            source_path.write_text("conteudo", encoding="utf-8")
            report_path = root / "runtime" / "ingest_failures.json"
            report_path.parent.mkdir()
            url = "https://docs.allowed.invalid/manual"

            with patch.object(config, "FAILED_INGEST_REPORT", str(report_path)):
                ingest._save_failed_report_entry(
                    filename="manual.md",
                    source=str(source_path),
                    total_chunks=2,
                    failed_chunks=[1],
                    source_type="file",
                )
                ingest._save_failed_report_entry(
                    filename="web-source",
                    source=url,
                    total_chunks=1,
                    failed_chunks=[0],
                    source_type="url",
                )
                file_sources, url_sources = ingest._failed_sources_from_report()

            report = json.loads(report_path.read_text(encoding="utf-8"))

        self.assertEqual(report["files"]["manual.md"]["source"], str(source_path))
        self.assertEqual(report["files"]["web-source"]["source"], url)
        self.assertEqual(file_sources, [source_path])
        self.assertEqual(url_sources, [url])


class TestDatabaseCleanupLogSanitization(unittest.TestCase):
    def test_rollback_cleanup_omits_traceback_and_error_text(self):
        secret = "senha-super-secreta"

        class Cursor:
            description = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, *_args):
                raise RuntimeError(secret)

        class Connection:
            def cursor(self):
                return Cursor()

            def rollback(self):
                raise ValueError(secret)

        with (
            patch.object(
                db,
                "_acquire_connection",
                return_value=nullcontext(Connection()),
            ),
            self.assertLogs(db.logger, level=logging.DEBUG) as captured,
        ):
            with self.assertRaisesRegex(RuntimeError, secret):
                db._fetch_rows("SELECT 1")

        logs = "\n".join(captured.output)
        self.assertIn("stage=rollback", logs)
        self.assertIn("error_type=ValueError", logs)
        self.assertNotIn(secret, logs)


if __name__ == "__main__":
    unittest.main()
