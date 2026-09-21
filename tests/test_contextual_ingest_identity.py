import unittest
from types import SimpleNamespace
from unittest.mock import patch

import config
import ingest


class TestContextualIngestIdentity(unittest.TestCase):
    @staticmethod
    def _processing_hash() -> str:
        section = ingest.AnalyticalSection(
            section_index=0,
            title="Manual",
            heading_path="Manual",
            content="Conteudo",
            module="geral",
            answer_mode="general",
            entities={},
            semantic_context="",
        )
        with patch.object(ingest, "_document_sections_supported", return_value=False):
            return ingest._processing_hash(
                content_hash="content-hash",
                title="Manual",
                doc_type="md",
                module="geral",
                doc_priority=5,
                sections=[section],
                chunk_items=[(0, "Conteudo", "Conteudo", section)],
            )

    def test_effective_contextual_provider_and_model_invalidate_hash(self):
        with patch.multiple(
            config,
            CONTEXTUAL_RETRIEVAL_ENABLED=True,
            GENERATION_PROVIDER="gemini",
            GENERATION_MODEL="gemini-response",
            CONTEXTUAL_RETRIEVAL_MODEL="gemini-contextual",
        ):
            gemini_hash = self._processing_hash()
            repeated_hash = self._processing_hash()

        with patch.multiple(
            config,
            CONTEXTUAL_RETRIEVAL_ENABLED=True,
            GENERATION_PROVIDER="openai",
            GENERATION_MODEL="gpt-response",
            CONTEXTUAL_RETRIEVAL_MODEL="gemini-contextual",
            OPENAI_CONTEXTUAL_MODEL="gpt-contextual",
        ):
            openai_hash = self._processing_hash()

        self.assertEqual(gemini_hash, repeated_hash)
        self.assertNotEqual(gemini_hash, openai_hash)

    def test_response_model_alone_does_not_invalidate_contextual_hash(self):
        with patch.multiple(
            config,
            CONTEXTUAL_RETRIEVAL_ENABLED=True,
            GENERATION_PROVIDER="gemini",
            GENERATION_MODEL="gemini-response-a",
            CONTEXTUAL_RETRIEVAL_MODEL="gemini-contextual",
        ):
            first_hash = self._processing_hash()

        with patch.multiple(
            config,
            CONTEXTUAL_RETRIEVAL_ENABLED=True,
            GENERATION_PROVIDER="gemini",
            GENERATION_MODEL="gemini-response-b",
            CONTEXTUAL_RETRIEVAL_MODEL="gemini-contextual",
        ):
            second_hash = self._processing_hash()

        self.assertEqual(first_hash, second_hash)

    def test_prompt_contract_change_invalidates_hash(self):
        with patch.object(config, "CONTEXTUAL_RETRIEVAL_ENABLED", True):
            first_hash = self._processing_hash()
            with patch.object(
                ingest,
                "_CONTEXTUAL_INSTRUCTIONS",
                ingest._CONTEXTUAL_INSTRUCTIONS + " Regra nova.",
            ):
                changed_hash = self._processing_hash()

        self.assertNotEqual(first_hash, changed_hash)

    def test_input_and_output_limits_reach_provider_call(self):
        full_document = "a" * 1500
        provider_response = SimpleNamespace(text='["contexto"]')
        with (
            patch.multiple(
                config,
                CONTEXTUAL_RETRIEVAL_ENABLED=True,
                CONTEXTUAL_RETRIEVAL_MAX_DOC_CHARS=1000,
                CONTEXTUAL_RETRIEVAL_MAX_TOKENS=123,
            ),
            patch.object(ingest, "_wait_for_contextual_slot"),
            patch("rag._gemini_generate", return_value=provider_response) as generate,
        ):
            result = ingest._contextualize_chunks_batch(
                chunks_with_indices=[(0, "trecho")],
                full_document=full_document,
                filename="manual.md",
            )

        call = generate.call_args.kwargs
        sent_document = call["contents"].split("<documento>\n", 1)[1].split(
            "\n</documento>",
            1,
        )[0]
        self.assertEqual(result, [(0, "contexto\n\ntrecho")])
        self.assertEqual(call["max_tokens"], 123)
        self.assertEqual(sent_document, full_document[:1000])

    def test_contextual_and_embedding_batches_are_independent(self):
        section = ingest.AnalyticalSection(
            section_index=0,
            title="Manual",
            heading_path="Manual",
            content="Conteudo",
            module="geral",
            answer_mode="general",
            entities={},
            semantic_context="",
        )
        chunk_items = [
            (index, f"original-{index}", f"retrieval-{index}", section)
            for index in range(5)
        ]
        contextual_batch_sizes = []
        embedding_batch_sizes = []
        embedded_contents = []

        def contextualize(**kwargs):
            pairs = kwargs["chunks_with_indices"]
            contextual_batch_sizes.append(len(pairs))
            return [(index, f"contexto\n\n{content}") for index, content in pairs]

        def embed(contents, *_args, **_kwargs):
            embedding_batch_sizes.append(len(contents))
            embedded_contents.extend(contents)
            return [[0.001] * config.EMBEDDING_DIMENSIONS for _content in contents]

        with (
            patch.multiple(
                config,
                CONTEXTUAL_RETRIEVAL_ENABLED=True,
                CONTEXTUAL_RETRIEVAL_BATCH_SIZE=3,
                EMBEDDING_BATCH_SIZE=2,
            ),
            patch.object(ingest, "ensure_embedding_index_identity"),
            patch.object(
                ingest,
                "_contextualize_chunks_batch",
                side_effect=contextualize,
            ),
            patch.object(ingest, "_embed_batch_with_retry", side_effect=embed),
        ):
            rows, failed = ingest._prepare_chunk_rows(
                doc_id="documento-1",
                filename="manual.md",
                title="Manual",
                text="documento completo",
                doc_type="md",
                source_type="file",
                module="geral",
                doc_priority=5,
                chunk_items=chunk_items,
            )

        self.assertEqual(contextual_batch_sizes, [3, 2])
        self.assertEqual(embedding_batch_sizes, [2, 2, 1])
        self.assertTrue(
            all(content.startswith("contexto\n\nretrieval-") for content in embedded_contents)
        )
        self.assertEqual([row["content"] for row in rows], [
            f"original-{index}" for index in range(5)
        ])
        self.assertEqual(
            [row["retrieval_text"] for row in rows],
            embedded_contents,
        )
        self.assertTrue(
            all(
                row["contextualization_version"]
                == ingest._CONTEXTUAL_RETRIEVAL_CONTRACT_VERSION
                for row in rows
            )
        )
        self.assertEqual(failed, [])

    def test_non_contextualized_chunk_keeps_retrieval_text_without_version(self):
        section = ingest.AnalyticalSection(
            section_index=0,
            title="Manual",
            heading_path="Manual",
            content="Conteudo",
            module="geral",
            answer_mode="general",
            entities={},
            semantic_context="",
        )

        with (
            patch.multiple(
                config,
                CONTEXTUAL_RETRIEVAL_ENABLED=False,
                EMBEDDING_BATCH_SIZE=10,
            ),
            patch.object(ingest, "ensure_embedding_index_identity"),
            patch.object(
                ingest,
                "_embed_batch_with_retry",
                return_value=[[0.001] * config.EMBEDDING_DIMENSIONS],
            ) as embed,
        ):
            rows, failed = ingest._prepare_chunk_rows(
                doc_id="documento-1",
                filename="manual.md",
                title="Manual",
                text="documento completo",
                doc_type="md",
                source_type="file",
                module="geral",
                doc_priority=5,
                chunk_items=[(0, "original", "cabecalho\n\noriginal", section)],
            )

        self.assertEqual(embed.call_args.args[0], ["cabecalho\n\noriginal"])
        self.assertEqual(rows[0]["content"], "original")
        self.assertEqual(rows[0]["retrieval_text"], "cabecalho\n\noriginal")
        self.assertIsNone(rows[0]["contextualization_version"])
        self.assertEqual(failed, [])


if __name__ == "__main__":
    unittest.main()
