import unittest
from uuid import uuid4
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

import config
import db
import ingest


class TestAnalyticalIngest(unittest.TestCase):
    def test_collect_local_files_only_reads_documents_root(self):
        with TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "base.md").write_text("# Base\n\nConteudo", encoding="utf-8")
            (root / "docbkp").mkdir()
            (root / "docbkp" / "backup.md").write_text("# Backup\n\nConteudo", encoding="utf-8")
            (root / "rascunhos").mkdir()
            (root / "rascunhos" / "rascunho.md").write_text("# Rascunho\n\nConteudo", encoding="utf-8")

            _docs_path, files = ingest._collect_local_files(directory=str(root), recursive=True)

        names = sorted(path.name for path in files)
        self.assertEqual(names, ["base.md"])

    def test_json_safe_normalizes_nested_uuid_values(self):
        document_id = uuid4()
        nested_id = uuid4()

        payload = {
            "document_id": document_id,
            "items": [nested_id, {"inner": nested_id}],
        }

        normalized = db._json_safe(payload)

        self.assertEqual(normalized["document_id"], str(document_id))
        self.assertEqual(normalized["items"][0], str(nested_id))
        self.assertEqual(normalized["items"][1]["inner"], str(nested_id))

    def test_extracts_entities_and_answer_mode_from_markdown_section(self):
        text = (
            "# Base maxPedido\n\n"
            "## Pedido nao aparece no ERP\n\n"
            "Validar a tabela MXSINTEGRACAOPEDIDO e o endpoint /api/v1/StatusPedidos.\n"
            "Parametro `PRAZO_VALIDADE_PEDIDO` pode impactar envio.\n"
            "Erro 503 indica falha no envio.\n"
        )

        with patch.object(config, "ANALYTICAL_CONTEXT_ENABLED", True):
            sections = ingest._split_markdown_sections(
                text,
                doc_title="Base maxPedido",
                base_module="pedidos_vendas",
                doc_type="md",
            )

        target = next(section for section in sections if section.title == "Pedido nao aparece no ERP")
        self.assertEqual(target.answer_mode, "troubleshooting")
        self.assertIn("MXSINTEGRACAOPEDIDO", target.entities["tables"])
        self.assertIn("PRAZO_VALIDADE_PEDIDO", target.entities["parameters"])
        self.assertIn("/api/v1/StatusPedidos", target.entities["endpoints"])
        self.assertIn("Pedido nao aparece no ERP", target.heading_path)
        self.assertIn("Modo de resposta: troubleshooting", target.semantic_context)

    def test_infers_configuration_answer_mode(self):
        answer_mode = ingest._infer_answer_mode(
            "Habilitar parametro `USAGRADE` na configuracao do pedido.",
            "Grade de produto",
        )

        self.assertEqual(answer_mode, "configuration")

    def test_build_chunk_row_includes_analytical_fields(self):
        section = ingest.AnalyticalSection(
            section_index=2,
            title="Pedido nao integra",
            heading_path="Pedidos > Pedido nao integra",
            content="Validar MXSINTEGRACAOPEDIDO.",
            module="sql_integracao",
            answer_mode="troubleshooting",
            entities={"tables": ["MXSINTEGRACAOPEDIDO"], "parameters": [], "endpoints": [], "statuses": [], "error_terms": []},
            semantic_context="Secao: Pedidos > Pedido nao integra | Tabelas: MXSINTEGRACAOPEDIDO",
            section_id="11111111-1111-1111-1111-111111111111",
        )

        row = ingest._build_chunk_row(
            doc_id="doc-1",
            chunk_index=0,
            clean_content="Conteudo do chunk",
            filename="base.md",
            doc_type="md",
            source_type="file",
            module="pedidos_vendas",
            title="Base",
            doc_priority=10,
            embedding=[0.1, 0.2],
            section=section,
        )

        self.assertEqual(row["section_id"], section.section_id)
        self.assertEqual(row["heading_path"], section.heading_path)
        self.assertEqual(row["answer_mode"], "troubleshooting")
        self.assertEqual(row["entities"]["tables"], ["MXSINTEGRACAOPEDIDO"])
        self.assertEqual(row["metadata"]["section_title"], "Pedido nao integra")
        self.assertEqual(row["metadata"]["module"], "sql_integracao")

    def test_build_section_retrieval_text_includes_operational_signals(self):
        section = ingest.AnalyticalSection(
            section_index=1,
            title="Pedido bloqueado",
            heading_path="Pedidos > Pedido bloqueado",
            content="Validar a tabela MXSINTEGRACAOPEDIDO e o parametro PEDIDO_BLOQUEADO.",
            module="pedidos_vendas",
            answer_mode="troubleshooting",
            entities={
                "tables": ["MXSINTEGRACAOPEDIDO"],
                "parameters": ["PEDIDO_BLOQUEADO"],
                "endpoints": [],
                "statuses": [],
                "error_terms": ["pedido bloqueado"],
            },
            semantic_context="Secao operacional de bloqueio de pedidos",
        )

        retrieval_text = ingest._build_section_retrieval_text("Base MaxPedido", section)

        self.assertIn("Documento: Base MaxPedido", retrieval_text)
        self.assertIn("Secao: Pedidos > Pedido bloqueado", retrieval_text)
        self.assertIn("Modo de resposta: troubleshooting", retrieval_text)
        self.assertIn("Tabelas: MXSINTEGRACAOPEDIDO", retrieval_text)
        self.assertIn("Parametros: PEDIDO_BLOQUEADO", retrieval_text)


if __name__ == "__main__":
    unittest.main()
