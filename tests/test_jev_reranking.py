import copy
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import config
import jev
import rag


def _chunk(name, similarity):
    return {
        "id": name,
        "document_id": f"doc-{name}",
        "section_id": f"section-{name}",
        "filename": f"{name}.md",
        "content": f"Procedimento documental completo para {name}.",
        "similarity": similarity,
        "metadata": {"content_hash": f"hash-{name}", "scope": {"guild_id": "123"}},
    }


def _decision(candidate_id, score=None, status="ok"):
    return jev.DecisionResult(
        status=status,
        model_requested="jev-1.13.0",
        model_effective="jev-1.13.0" if status == "ok" else None,
        answers={"relevance": {"type": "noul", "noul": score}} if status == "ok" else {},
        usage={"input_tokens": 100, "output_tokens": 0},
        latency_ms=1,
        error_code=None if status == "ok" else status,
        request_id="request-1",
        call_id=f"call-{candidate_id}",
        candidate_id=candidate_id,
        estimated_cost_usd=0.0000042,
        cost_status="estimated",
    )


class _FakeClient:
    def __init__(self, scores):
        self.scores = iter(scores)
        self.calls = []

    def decide(self, state, questions, **kwargs):
        self.calls.append((state, questions, kwargs))
        score = next(self.scores)
        if isinstance(score, str):
            return _decision(kwargs["candidate_id"], status=score)
        return _decision(kwargs["candidate_id"], score=score)

    def close(self):
        pass


class _BatchFakeClient:
    def __init__(self, batches):
        self.batches = iter(batches)
        self.calls = []

    def decide(self, state, questions, **kwargs):
        self.calls.append((state, questions, kwargs))
        batch = next(self.batches)
        if isinstance(batch, str):
            return _decision(kwargs["candidate_id"], status=batch)
        answers = {
            question_id: {"type": "noul", "noul": score}
            for question_id, score in zip(questions, batch)
        }
        return jev.DecisionResult(
            status="ok",
            model_requested="jev-1.13.0",
            model_effective="jev-1.13.0",
            answers=answers,
            usage={"input_tokens": 100, "output_tokens": 0},
            latency_ms=1,
            error_code=None,
            request_id="request-1",
            call_id=f"call-{len(self.calls)}",
            candidate_id=kwargs["candidate_id"],
            estimated_cost_usd=0.0000042,
            cost_status="estimated",
        )

    def close(self):
        pass


class TestJevReranking(unittest.TestCase):
    def setUp(self):
        self.chunks = [_chunk("a", 0.95), _chunk("b", 0.70), _chunk("c", 0.60)]
        self.config_patch = patch.multiple(
            config,
            RAG_ENABLE_RERANKING=True,
            RAG_RERANK_PROVIDER="jev",
            JEV_RERANK_MAX_CANDIDATES=2,
            JEV_RERANK_MODE="pointwise",
            JEV_MAX_STATE_ESTIMATED_TOKENS=24000,
            JEV_STAGE_TIMEOUT_SECONDS=8.0,
            JEV_MIN_REMAINING_SECONDS=1.0,
            JEV_MODEL="jev-1.13.0",
            TYPESAFE_API_KEY="fake-test-key",
        )
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)

    def test_jev_inverts_similarity_and_changes_context_without_mutating_chunks(self):
        original = copy.deepcopy(self.chunks)
        fake = _FakeClient([0.0, 0.9])
        model_calls = []
        trace = {}
        with patch("rag.jev.TypeSafeClient", return_value=fake), patch(
            "rag._rerank_chunks_with_llm", side_effect=AssertionError("existing called")
        ):
            ranked = rag._rerank_chunks(
                "Como configurar?", self.chunks, request_id="request-1", model_calls=model_calls, trace=trace
            )

        self.assertEqual([chunk["id"] for chunk in ranked], ["b", "a", "c"])
        self.assertEqual(self.chunks, original)
        self.assertEqual(ranked[0]["jev"]["original_rank"], 2)
        self.assertEqual(ranked[0]["jev"]["reranked_rank"], 1)
        self.assertEqual(len(ranked[0]["jev"]["state_sha256"]), 64)
        self.assertEqual(ranked[1]["jev"]["relevance"], 0.0)
        self.assertEqual(ranked[0]["metadata"], original[1]["metadata"])
        self.assertNotIn("jev", ranked[2])
        context = rag.build_context(ranked)
        self.assertLess(context.index('source="b.md"'), context.index('source="a.md"'))
        self.assertEqual([call["provider"] for call in model_calls], ["typesafe", "typesafe"])
        self.assertTrue(trace["rerank"][0]["applied"])
        self.assertEqual(trace["rerank"][0]["effective_provider"], "jev")
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(fake.calls[1][0]["trecho_documental"], original[1]["content"])
        self.assertNotIn("candidate_id", fake.calls[1][0])
        self.assertNotIn("retrieval_text", str(fake.calls))

    def test_partial_failure_falls_back_integrally_to_existing_policy(self):
        self.chunks[0]["similarity"] = 0.70
        fake = _FakeClient([0.99, "timeout"])
        trace = {}
        with patch("rag.jev.TypeSafeClient", return_value=fake), patch(
            "rag._rerank_chunks_with_llm", return_value=list(reversed(self.chunks))
        ) as existing:
            ranked = rag._rerank_chunks("Pergunta", self.chunks, trace=trace)
        self.assertEqual(ranked, list(reversed(self.chunks)))
        existing.assert_called_once()
        self.assertTrue(all("jev" not in chunk for chunk in ranked))
        self.assertEqual(trace["rerank"][0]["fallback_reason"], "timeout")
        self.assertEqual(trace["rerank"][0]["effective_provider"], "existing")
        self.assertTrue(trace["rerank"][0]["applied"])

    def test_oversize_candidate_skips_entire_passage(self):
        self.chunks[1]["content"] = "x" * 2000
        with patch.object(config, "JEV_MAX_STATE_ESTIMATED_TOKENS", 100), patch(
            "rag.jev.TypeSafeClient", side_effect=AssertionError("client constructed")
        ), patch("rag._rerank_chunks_with_llm", return_value=self.chunks):
            ranked, summary = rag._rerank_chunks_with_jev("Pergunta", self.chunks)
        self.assertIs(ranked, self.chunks)
        self.assertEqual(summary["fallback_reason"], "state_limit")

    def test_ids_do_not_depend_on_retrieval_position(self):
        self.assertEqual(rag._jev_candidate_id(self.chunks[0]), rag._jev_candidate_id(copy.deepcopy(self.chunks[0])))
        changed = copy.deepcopy(self.chunks[0])
        changed["content"] += " Mudanca."
        self.assertNotEqual(rag._jev_candidate_id(self.chunks[0]), rag._jev_candidate_id(changed))

    def test_legacy_chunks_with_same_content_have_distinct_candidate_ids(self):
        first = {"filename": "a.md", "document_id": "doc", "chunk_index": 0, "content": "Mesmo texto"}
        second = {"filename": "a.md", "document_id": "doc", "chunk_index": 1, "content": "Mesmo texto"}
        third = {"filename": "b.md", "document_id": "doc", "chunk_index": 0, "content": "Mesmo texto"}
        self.assertEqual(len(rag._dedupe_chunks([first, second, third])), 3)
        self.assertEqual(len({rag._jev_candidate_id(chunk) for chunk in (first, second, third)}), 3)

    def test_disabled_and_existing_preserve_existing_path(self):
        trace = {}
        with patch.object(config, "RAG_RERANK_PROVIDER", "existing"), patch(
            "rag._rerank_chunks_with_llm", return_value=self.chunks
        ) as existing, patch("rag.jev.TypeSafeClient", side_effect=AssertionError("Jev called")):
            self.assertIs(rag._rerank_chunks("Pergunta", self.chunks, trace=trace), self.chunks)
        existing.assert_called_once()
        self.assertIsNone(trace["rerank"][0]["candidate_count"])

        with patch.object(config, "RAG_ENABLE_RERANKING", False), patch(
            "rag._rerank_chunks_with_llm", return_value=self.chunks
        ) as existing, patch("rag.jev.TypeSafeClient", side_effect=AssertionError("Jev called")):
            self.assertIs(rag._rerank_chunks("Pergunta", self.chunks), self.chunks)
        existing.assert_called_once()

    def test_existing_fallback_uses_deadline_before_generation_reserve(self):
        self.chunks[0]["similarity"] = 0.70
        fake = _FakeClient(["timeout"])
        global_deadline = time.monotonic() + 1.0
        token = rag._request_deadline.set(global_deadline)
        seen = []

        def slow_provider(**kwargs):
            seen.append((rag._request_deadline.get(), rag._remaining_request_timeout(120)))
            raise TimeoutError("provider lento")

        try:
            with patch.object(config, "JEV_MIN_REMAINING_SECONDS", 0.5), patch(
                "rag.jev.TypeSafeClient", return_value=fake
            ), patch("rag._gemini_generate", side_effect=slow_provider):
                ranked = rag._rerank_chunks("Pergunta", self.chunks)
            self.assertIs(ranked, self.chunks)
            self.assertEqual(len(seen), 1)
            self.assertAlmostEqual(seen[0][0], global_deadline - 0.5, delta=0.02)
            self.assertLessEqual(seen[0][1], 0.5)
            self.assertEqual(rag._request_deadline.get(), global_deadline)
        finally:
            rag._request_deadline.reset(token)

    def test_slow_existing_provider_cannot_exhaust_global_reserve(self):
        self.chunks[0]["similarity"] = 0.70
        clock = {"now": 0.0}
        token = rag._request_deadline.set(10.0)

        def slow_provider(**kwargs):
            self.assertEqual(rag._remaining_request_timeout(120), 7.0)
            clock["now"] = 7.1
            rag._ensure_request_active("rerank")

        try:
            with patch.object(config, "JEV_MIN_REMAINING_SECONDS", 3.0), patch.object(
                rag, "_time", SimpleNamespace(monotonic=lambda: clock["now"])
            ), patch("rag.jev.TypeSafeClient", return_value=_FakeClient(["timeout"])), patch(
                "rag._gemini_generate", side_effect=slow_provider
            ):
                ranked = rag._rerank_chunks("Pergunta", self.chunks)
            self.assertIs(ranked, self.chunks)
            self.assertEqual(rag._request_deadline.get(), 10.0)
            self.assertLess(clock["now"], 10.0)
        finally:
            rag._request_deadline.reset(token)

    def test_late_existing_success_does_not_apply_ranking(self):
        self.chunks[0]["similarity"] = 0.70
        clock = {"now": 0.0}
        token = rag._request_deadline.set(10.0)
        trace = {}

        def late_success(**kwargs):
            self.assertEqual(rag._request_deadline.get(), 7.0)
            clock["now"] = 9.0
            return SimpleNamespace(text="2,1,0")

        try:
            with patch.object(config, "JEV_MIN_REMAINING_SECONDS", 3.0), patch.object(
                rag, "_time", SimpleNamespace(monotonic=lambda: clock["now"])
            ), patch("rag.jev.TypeSafeClient", return_value=_FakeClient(["timeout"])), patch(
                "rag._gemini_generate", side_effect=late_success
            ):
                ranked = rag._rerank_chunks("Pergunta", self.chunks, trace=trace)
            self.assertIs(ranked, self.chunks)
            self.assertFalse(trace["rerank"][0]["applied"])
            self.assertEqual(trace["rerank"][0]["effective_provider"], "retrieval")
            self.assertEqual(rag._request_deadline.get(), 10.0)
        finally:
            rag._request_deadline.reset(token)

    def test_state_cap_rejects_value_above_conservative_limit(self):
        with patch.object(config, "JEV_MAX_STATE_ESTIMATED_TOKENS", 50000):
            with self.assertRaisesRegex(EnvironmentError, "JEV_MAX_STATE_ESTIMATED_TOKENS"):
                config.validate_jev_config(active=True)

    def test_deadline_reserve_skips_jev(self):
        token = rag._request_deadline.set(time.monotonic() + 0.1)
        try:
            with patch("rag.jev.TypeSafeClient", side_effect=AssertionError("Jev called")):
                ranked, summary = rag._rerank_chunks_with_jev("Pergunta", self.chunks)
        finally:
            rag._request_deadline.reset(token)
        self.assertIs(ranked, self.chunks)
        self.assertEqual(summary["fallback_reason"], "deadline_reserve")

    def test_invalid_active_configuration_is_not_silent_fallback(self):
        with patch.object(config, "TYPESAFE_API_KEY", ""), patch(
            "rag._rerank_chunks_with_llm", side_effect=AssertionError("fallback called")
        ):
            with self.assertRaisesRegex(EnvironmentError, "TYPESAFE_API_KEY"):
                rag._rerank_chunks("Pergunta", self.chunks)
            with self.assertRaisesRegex(EnvironmentError, "TYPESAFE_API_KEY"):
                rag._rerank_chunks("Pergunta", self.chunks[:1])

    def test_equal_scores_preserve_order_and_tail(self):
        fake = _FakeClient([0.5, 0.5])
        with patch("rag.jev.TypeSafeClient", return_value=fake):
            ranked, summary = rag._rerank_chunks_with_jev("Pergunta", self.chunks)
        self.assertEqual([chunk["id"] for chunk in ranked], ["a", "b", "c"])
        self.assertIs(ranked[2], self.chunks[2])
        self.assertEqual(summary["excluded_by_cap_count"], 1)

        batch_fake = _BatchFakeClient([[0.5, 0.5]])
        with patch.object(config, "JEV_RERANK_MODE", "batch"), patch(
            "rag.jev.TypeSafeClient", return_value=batch_fake
        ):
            batch_ranked, batch_summary = rag._rerank_chunks_with_jev(
                "Pergunta", self.chunks
            )
        self.assertEqual([chunk["id"] for chunk in batch_ranked], ["a", "b", "c"])
        self.assertIs(batch_ranked[2], self.chunks[2])
        self.assertEqual(batch_summary["excluded_by_cap_count"], 1)

    def test_batch_scores_all_candidates_in_one_request_with_traceable_associations(self):
        fake = _BatchFakeClient([[0.1, 0.9, 0.2]])
        model_calls = []
        with patch.multiple(
            config, JEV_RERANK_MODE="batch", JEV_RERANK_MAX_CANDIDATES=3
        ), patch("rag.jev.TypeSafeClient", return_value=fake):
            ranked, summary = rag._rerank_chunks_with_jev(
                "Como configurar?", self.chunks, request_id="request-1", model_calls=model_calls
            )

        self.assertEqual([chunk["id"] for chunk in ranked], ["b", "c", "a"])
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(len(fake.calls[0][0]["candidatos"]), 3)
        self.assertEqual(len(fake.calls[0][1]), 3)
        for index, question in enumerate(fake.calls[0][1].values()):
            candidate_id = fake.calls[0][0]["candidatos"][index]["candidate_id"]
            self.assertIn(candidate_id, question["instructions"]["candidate_reference"])
        self.assertEqual(summary["mode"], "batch")
        self.assertEqual(summary["batch_count"], 1)
        self.assertEqual(summary["calls_completed"], 1)
        self.assertEqual(summary["decisions_completed"], 3)
        self.assertEqual(summary["estimated_cost_usd"], 0.0000042)
        self.assertEqual(len(model_calls), 1)
        self.assertEqual(len(model_calls[0]["candidate_ids"]), 3)
        self.assertEqual(model_calls[0]["call_id"], summary["calls"][0]["call_id"])
        self.assertEqual(len({chunk["jev"]["state_sha256"] for chunk in ranked}), 1)
        self.assertEqual(len({chunk["jev"]["call_id"] for chunk in ranked}), 1)
        self.assertEqual(ranked[0]["metadata"], self.chunks[1]["metadata"])
        self.assertNotIn("Como configurar?", str(summary))
        self.assertNotIn("Procedimento documental", str(summary))

    def test_batch_groups_consecutive_candidates_without_reordering_composition(self):
        fake = _BatchFakeClient([[0.2, 0.9], [0.8]])

        def estimate(value, **_kwargs):
            candidate_count = str(value).count('"trecho_documental"')
            return (200 if candidate_count > 2 else 100), rag.TOKEN_COUNTER_VERSION

        with patch.multiple(
            config,
            JEV_RERANK_MODE="batch",
            JEV_RERANK_MAX_CANDIDATES=3,
            JEV_MAX_STATE_ESTIMATED_TOKENS=100,
        ), patch("rag._count_context_text", side_effect=estimate), patch(
            "rag.jev.TypeSafeClient", return_value=fake
        ):
            ranked, summary = rag._rerank_chunks_with_jev("Pergunta", self.chunks)

        self.assertEqual(len(fake.calls), 2)
        self.assertEqual(
            [[candidate["fonte"] for candidate in call[0]["candidatos"]] for call in fake.calls],
            [["a.md", "b.md"], ["c.md"]],
        )
        self.assertEqual([chunk["id"] for chunk in ranked], ["b", "c", "a"])
        self.assertEqual(summary["batch_count"], 2)
        self.assertEqual(summary["calls_completed"], 2)

    def test_failure_in_later_batch_discards_all_batch_scores(self):
        fake = _BatchFakeClient([[0.2, 0.9], "timeout"])

        def estimate(value, **_kwargs):
            candidate_count = str(value).count('"trecho_documental"')
            return (200 if candidate_count > 2 else 100), rag.TOKEN_COUNTER_VERSION

        with patch.multiple(
            config,
            JEV_RERANK_MODE="batch",
            JEV_RERANK_MAX_CANDIDATES=3,
            JEV_MAX_STATE_ESTIMATED_TOKENS=100,
        ), patch("rag._count_context_text", side_effect=estimate), patch(
            "rag.jev.TypeSafeClient", return_value=fake
        ):
            ranked, summary = rag._rerank_chunks_with_jev("Pergunta", self.chunks)

        self.assertIs(ranked, self.chunks)
        self.assertEqual(summary["fallback_reason"], "timeout")
        self.assertEqual(summary["calls_completed"], 1)
        self.assertEqual(summary["decisions_completed"], 2)
        self.assertEqual(len(summary["calls"]), 2)

    def test_batch_rejects_response_with_missing_question_id(self):
        class InvalidClient(_BatchFakeClient):
            def decide(self, state, questions, **kwargs):
                result = super().decide(state, questions, **kwargs)
                result.answers.pop(next(iter(result.answers)))
                return result

        with patch.multiple(
            config, JEV_RERANK_MODE="batch", JEV_RERANK_MAX_CANDIDATES=3
        ), patch("rag.jev.TypeSafeClient", return_value=InvalidClient([[0.1, 0.9, 0.2]])):
            ranked, summary = rag._rerank_chunks_with_jev("Pergunta", self.chunks)

        self.assertIs(ranked, self.chunks)
        self.assertEqual(summary["fallback_reason"], "invalid_response")

    def test_isolated_oversize_candidate_prevents_all_batch_calls(self):
        self.chunks[1]["content"] = "x" * 2000
        with patch.multiple(
            config,
            JEV_RERANK_MODE="batch",
            JEV_RERANK_MAX_CANDIDATES=3,
            JEV_MAX_STATE_ESTIMATED_TOKENS=100,
        ), patch("rag.jev.TypeSafeClient", side_effect=AssertionError("client constructed")):
            ranked, summary = rag._rerank_chunks_with_jev("Pergunta", self.chunks)

        self.assertIs(ranked, self.chunks)
        self.assertEqual(summary["fallback_reason"], "state_limit")

    def test_batch_response_after_shared_deadline_reserve_is_not_applied(self):
        clock = {"now": 0.0}
        fake = _BatchFakeClient([[0.1, 0.9, 0.2]])
        original_decide = fake.decide

        def late_decide(*args, **kwargs):
            clock["now"] = 7.1
            return original_decide(*args, **kwargs)

        fake.decide = late_decide
        token = rag._request_deadline.set(10.0)
        try:
            with patch.multiple(
                config,
                JEV_RERANK_MODE="batch",
                JEV_RERANK_MAX_CANDIDATES=3,
                JEV_MIN_REMAINING_SECONDS=3.0,
            ), patch.object(
                rag, "_time", SimpleNamespace(monotonic=lambda: clock["now"])
            ), patch("rag.jev.TypeSafeClient", return_value=fake):
                ranked, summary = rag._rerank_chunks_with_jev("Pergunta", self.chunks)
        finally:
            rag._request_deadline.reset(token)

        self.assertIs(ranked, self.chunks)
        self.assertEqual(summary["fallback_reason"], "stage_budget_exhausted")
        self.assertEqual(summary["decisions_completed"], 0)

    def test_fake_ask_response_follows_jev_context_order(self):
        fake = _FakeClient([0.0, 0.9])
        contexts = []

        def answer_from_context(**kwargs):
            system = kwargs["evidence_context"]
            contexts.append(system)
            a = system.index("Procedimento documental completo para a.")
            b = system.index("Procedimento documental completo para b.")
            return "Resposta B" if b < a else "Resposta A"

        with patch.multiple(
            config,
            MAX_CONTEXT_CHUNKS=2,
            RAG_ENABLE_QUERY_REFORMULATION=False,
            RAG_STRICT_ABSTAIN=False,
            RAG_ENABLE_BUSINESS_RULES=False,
            RAG_ENABLE_GROUNDING_VALIDATION=False,
            FULL_CONTEXT_ENABLED=False,
        ), patch("rag.jev.TypeSafeClient", return_value=fake), patch(
            "rag._classify_query_intent",
            return_value={"intent": "general", "modules": [], "doc_types": []},
        ), patch("rag.retrieve_chunks_with_feedback", return_value=(self.chunks, [], self.chunks)), patch(
            "rag._ask_model", side_effect=answer_from_context
        ), patch("rag._apply_grounding_regeneration", side_effect=lambda **kwargs: (
            kwargs["answer"], [], set(), 0
        )):
            enabled_answer, enabled_chunks, enabled_trace = rag.ask("Pergunta")
            with patch.object(config, "RAG_ENABLE_RERANKING", False):
                disabled_answer, disabled_chunks, disabled_trace = rag.ask("Pergunta")

        self.assertEqual(enabled_answer, "Resposta B")
        self.assertEqual(disabled_answer, "Resposta A")
        self.assertEqual([chunk["id"] for chunk in enabled_chunks], ["b", "a"])
        self.assertEqual([chunk["id"] for chunk in disabled_chunks], ["a", "b"])
        self.assertTrue(enabled_trace["rerank"][0]["applied"])
        self.assertFalse(disabled_trace["rerank"][0]["applied"])
        self.assertLess(contexts[0].index("Procedimento documental completo para b."), contexts[0].index("Procedimento documental completo para a."))


if __name__ == "__main__":
    unittest.main()
