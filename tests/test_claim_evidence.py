import hashlib
import json
import unittest
from html import escape
from types import SimpleNamespace
from unittest.mock import patch

import config
import grounding
import rag


def _selection(*items):
    chunks = []
    evidence = []
    for evidence_id, content, source in items:
        chunks.append({"content": content, "filename": source})
        evidence.append({
            "evidence_id": evidence_id,
            "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "spans": [{"start": 0, "end": len(content), "source_length": len(content)}],
            "source": source,
            "location": {"filename": source, "chunk_index": 0},
        })
    return SimpleNamespace(
        retained_chunks=tuple(chunks),
        evidence=tuple(evidence),
        rendered_text="\n".join(escape(content, quote=False) for _, content, _ in items),
    )


def _claim(answer, text, *, claim_id="c1", evidence_ids=None, kind="factual"):
    start = answer.index(text)
    return {
        "id": claim_id,
        "text": text,
        "answer_spans": [{"start": start, "end": start + len(text)}],
        "evidence_ids": ["ev1"] if evidence_ids is None else evidence_ids,
        "kind": kind,
    }


def _parse(answer, claims, selection=None, *, max_claims=12):
    selection = selection or _selection(("ev1", "O parâmetro 42 ativa PIX.", "a.md"))
    index, fingerprint = grounding.build_evidence_index(
        selection.evidence, selection.retained_chunks, selection.rendered_text,
    )
    return grounding.parse_claim_set(
        answer=answer,
        raw_json=json.dumps({"claims": claims}, ensure_ascii=False),
        evidence_index=index,
        evidence_fingerprint=fingerprint,
        extractor_model="fake-model",
        max_claims=max_claims,
    )


class TestClaimEvidence(unittest.TestCase):
    def test_two_claims_in_one_sentence_cover_distinct_technical_clauses(self):
        answer = "O parâmetro 42 ativa PIX e a tabela X bloqueia SQL.\n\nFontes:\n- a.md"
        first = "O parâmetro 42 ativa PIX"
        second = "a tabela X bloqueia SQL."
        claims = [_claim(answer, first), _claim(answer, second, claim_id="c2")]
        result = _parse(answer, claims)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["uncovered_spans"], [])
        self.assertEqual(result["claims"][0]["evidence_refs"][0]["id"], "ev1")
        self.assertNotIn("claims", grounding.claim_set_trace(result))

        combined = _parse(answer, [_claim(answer, answer.split(".\n")[0] + ".")])
        self.assertEqual(combined["status"], "incomplete")
        self.assertEqual(combined["reason"], "multiple_technical_clauses")

    def test_omitted_condition_negation_and_sql_are_uncovered(self):
        answer = "Se NÃO houver saldo, execute SELECT 1; o parâmetro 42 permanece desligado."
        result = _parse(answer, [_claim(answer, "execute SELECT 1")])
        self.assertEqual(result["status"], "incomplete")
        uncovered = [answer[item["start"]:item["end"]] for item in result["uncovered_spans"]]
        self.assertIn("NÃO", uncovered)
        self.assertIn("42", uncovered)

    def test_unicode_offsets_are_characters_and_text_must_be_verbatim(self):
        answer = "📦 O parâmetro 42 está ativo."
        claim = _claim(answer, "O parâmetro 42 está ativo.")
        self.assertEqual(claim["answer_spans"][0]["start"], 2)
        self.assertEqual(_parse(answer, [claim])["status"], "complete")
        claim["text"] = "O parâmetro 43 está ativo."
        self.assertEqual(_parse(answer, [claim])["reason"], "claim_text_mismatch")
        claim["text"] = "O parâmetro 42 está ativo."
        claim["answer_spans"][0]["start"] = len(answer.encode("utf-8"))
        self.assertEqual(_parse(answer, [claim])["reason"], "invalid_answer_span")

    def test_missing_or_discarded_evidence_is_never_resolved_by_filename(self):
        answer = "O parâmetro 42 ativa PIX."
        selection = _selection(("ev1", "O parâmetro 42 ativa PIX.", "same.md"))
        missing = _parse(answer, [_claim(answer, answer, evidence_ids=["ev2"])], selection)
        self.assertEqual(missing["status"], "invalid")
        self.assertEqual(missing["invalid_reference_count"], 1)
        no_reference = _parse(answer, [_claim(answer, answer, evidence_ids=[])], selection)
        self.assertEqual(no_reference["status"], "incomplete")
        self.assertEqual(no_reference["reason"], "missing_evidence_refs")

    def test_ambiguous_basename_and_literal_quote_do_not_prove_support(self):
        answer = 'O parâmetro "42" ativa PIX.'
        selection = _selection(
            ("ev1", "O parâmetro 42 ativa PIX.", "same.md"),
            ("ev2", "Outro parâmetro 99 ativa PIX.", "same.md"),
        )
        result = _parse(answer, [_claim(answer, answer, evidence_ids=["ev2"])], selection)
        self.assertEqual(result["status"], "incomplete")
        self.assertEqual(result["reason"], "quote_mismatch")
        self.assertFalse(result["claims"][0]["quote_match"])
        self.assertEqual(result["claims"][0]["evidence_refs"][0]["id"], "ev2")
        literal = _parse(answer, [_claim(answer, answer, evidence_ids=["ev1"])], selection)
        self.assertEqual(literal["status"], "complete")
        self.assertTrue(literal["claims"][0]["quote_match"])

    def test_technical_non_factual_cap_duplicate_and_json_fail_closed(self):
        answer = "O parâmetro 42 ativa PIX."
        claim = _claim(answer, answer, kind="non_factual")
        self.assertEqual(_parse(answer, [claim])["status"], "incomplete")
        self.assertEqual(_parse(answer, [claim, claim])["reason"], "invalid_claim")
        self.assertEqual(_parse(answer, [claim, claim], max_claims=1)["reason"], "claim_cap_exceeded")
        index, fingerprint = grounding.build_evidence_index(
            _selection(("ev1", answer, "a.md")).evidence,
            _selection(("ev1", answer, "a.md")).retained_chunks,
            answer,
        )
        result = grounding.parse_claim_set(
            answer=answer, raw_json="not json", evidence_index=index,
            evidence_fingerprint=fingerprint, extractor_model="fake", max_claims=12,
        )
        self.assertEqual(result["status"], "invalid")

    def test_envelope_rejects_unrendered_or_changed_content(self):
        selection = _selection(("ev1", "Texto original", "a.md"))
        selection.retained_chunks[0]["content"] = "Texto alterado"
        with self.assertRaisesRegex(ValueError, "invalid_evidence_ref"):
            grounding.build_evidence_index(
                selection.evidence, selection.retained_chunks, selection.rendered_text,
            )

    def test_real_context_renderer_provides_resolvable_spans(self):
        chunk = {
            "id": "chunk-1", "content": "Campo <PIX> habilitado.",
            "filename": "a.md", "chunk_index": 0,
            "metadata": {"content_hash": "hash-do-documento-inteiro"},
        }
        records, _exclusions = rag._prepare_context_records(
            [chunk], max_chunks=2, max_per_section=2, max_per_document=2,
        )
        rendered, evidence = rag._render_context_records(records)
        index, fingerprint = grounding.build_evidence_index(evidence, [chunk], rendered)
        self.assertEqual(index["chunk-1"]["rendered_text"], chunk["content"])
        self.assertEqual(
            index["chunk-1"]["ref"]["content_hash"],
            hashlib.sha256(chunk["content"].encode("utf-8")).hexdigest(),
        )
        legacy_key = {**chunk, "id": ""}
        self.assertTrue(rag._context_chunk_key(legacy_key).endswith("hash-do-documento-inteiro"))
        self.assertEqual(len(fingerprint), 64)

    def test_zero_length_overlap_is_not_available_as_evidence(self):
        selection = _selection(
            ("ev1", "Campo 42", "a.md"),
            ("ev2", "Campo 42", "a.md"),
        )
        selection.evidence[1]["spans"] = [
            {"start": 8, "end": 8, "source_length": 8}
        ]
        index, _fingerprint = grounding.build_evidence_index(
            selection.evidence, selection.retained_chunks, selection.rendered_text,
        )
        self.assertEqual(list(index), ["ev1"])

    def test_extraction_uses_generation_provider_once_and_records_usage(self):
        answer = "O parâmetro 42 ativa PIX."
        selection = _selection(("ev1", answer, "a.md"))
        response = rag._GeneratedTextResponse(
            json.dumps({"claims": [_claim(answer, answer)]}, ensure_ascii=False),
            provider="openai", model="gpt-effective",
            usage={"input_tokens": 100, "output_tokens": 30},
        )
        calls = []
        with patch.multiple(
            config,
            GENERATION_PROVIDER="openai",
            GENERATION_MODEL="gpt-primary",
            GENERATION_MODEL_POLICY="primary",
            JEV_GROUNDING_MAX_CLAIMS=12,
        ), patch("rag._openai_chat_generate_request", return_value=response) as generate:
            result = rag._extract_answer_claims(
                question="Como ativar PIX?", answer=answer, selection=selection,
                model_calls=calls,
            )
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["extractor_model"], "gpt-effective")
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(generate.call_args.kwargs["model"], "gpt-primary")
        self.assertFalse(generate.call_args.kwargs["allow_compatibility_fallback"])
        self.assertEqual(calls[0]["stage"], "claim_extraction")
        self.assertEqual(calls[0]["usage"]["input_tokens"], 100)

    def test_deadline_prevents_call_and_marks_unavailable(self):
        answer = "O parâmetro 42 ativa PIX."
        selection = _selection(("ev1", answer, "a.md"))
        token = rag._request_deadline.set(0.0)
        try:
            with patch("rag._gemini_generate") as generate:
                result = rag._extract_answer_claims(
                    question="Como ativar?", answer=answer, selection=selection,
                )
            generate.assert_not_called()
        finally:
            rag._request_deadline.reset(token)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "deadline_exceeded")


if __name__ == "__main__":
    unittest.main()
