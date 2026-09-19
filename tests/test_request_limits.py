import asyncio
import threading
import time
import unittest
from unittest.mock import Mock, patch

import httpx

import config
import rag
from bot_common import InFlightTaskLimiter


class TestInFlightTaskLimiter(unittest.IsolatedAsyncioTestCase):
    async def test_timeout_da_espera_nao_libera_vaga_do_worker_lento(self):
        limiter = InFlightTaskLimiter(1)
        started = threading.Event()
        release = threading.Event()

        def slow_worker():
            started.set()
            release.wait(timeout=2)
            return "ok"

        worker = limiter.try_start(lambda: asyncio.to_thread(slow_worker))
        self.assertIsNotNone(worker)
        self.assertTrue(await asyncio.to_thread(started.wait, 1))

        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(worker), timeout=0.01)

        self.assertEqual(limiter.active_count, 1)
        rejected_factory = Mock()
        self.assertIsNone(limiter.try_start(rejected_factory))
        rejected_factory.assert_not_called()

        release.set()
        self.assertEqual(await worker, "ok")
        await asyncio.sleep(0)
        self.assertEqual(limiter.active_count, 0)

    async def test_cancelamento_da_espera_mantem_vaga_ate_finalizacao(self):
        limiter = InFlightTaskLimiter(1)
        release = asyncio.Event()
        worker = limiter.try_start(lambda: release.wait())

        async def wait_for_worker():
            return await asyncio.shield(worker)

        waiter = asyncio.create_task(wait_for_worker())
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter

        self.assertEqual(limiter.active_count, 1)
        release.set()
        await worker
        await asyncio.sleep(0)
        self.assertEqual(limiter.active_count, 0)

    async def test_excecao_libera_vaga_e_permite_uma_nova_task(self):
        limiter = InFlightTaskLimiter(1)

        async def fail():
            raise RuntimeError("falha controlada")

        worker = limiter.try_start(fail)
        with self.assertRaisesRegex(RuntimeError, "falha controlada"):
            await worker
        await asyncio.sleep(0)
        self.assertEqual(limiter.active_count, 0)

        next_worker = limiter.try_start(lambda: asyncio.sleep(0, result="ok"))
        self.assertEqual(await next_worker, "ok")


class TestRequestDeadline(unittest.TestCase):
    def _set_deadline(self, deadline: float):
        token = rag._request_deadline.set(deadline)
        self.addCleanup(rag._request_deadline.reset, token)

    def test_ask_expirado_nao_inicia_pipeline(self):
        with patch("rag._ask_impl") as ask_impl:
            with self.assertRaises(rag.RequestDeadlineExceeded):
                rag.ask("pergunta", deadline=time.monotonic() - 1)

        ask_impl.assert_not_called()

    def test_deadline_expirado_nao_inicia_rerank(self):
        self._set_deadline(time.monotonic() - 1)
        chunks = [
            {
                "id": str(index),
                "document_id": f"doc-{index}",
                "filename": f"doc-{index}.md",
                "content": "conteudo relevante",
                "similarity": 0.70,
            }
            for index in range(4)
        ]

        with patch.multiple(
            config,
            RAG_ENABLE_RERANKING=True,
            RERANKER_MIN_TRIGGER_SIM=0.55,
            RERANKER_MAX_TRIGGER_SIM=0.82,
        ), patch("rag._gemini_generate") as generate:
            with self.assertRaises(rag.RequestDeadlineExceeded):
                rag._rerank_chunks_with_llm("pergunta", chunks)

        generate.assert_not_called()

    def test_deadline_expirado_nao_inicia_regeneracao(self):
        self._set_deadline(time.monotonic() - 1)

        with patch.multiple(
            config,
            RAG_ENABLE_GROUNDING_VALIDATION=True,
            RAG_REQUIRE_SOURCES_SECTION=True,
            RAG_MAX_REGEN_ATTEMPTS=1,
        ), patch("rag._ask_model") as ask_model:
            with self.assertRaises(rag.RequestDeadlineExceeded):
                rag._apply_grounding_regeneration(
                    answer="Resposta sem fontes.",
                    question="Pergunta operacional?",
                    system="sistema",
                    conversation_history=None,
                    images=None,
                    allowed_sources={"guia.md"},
                )

        ask_model.assert_not_called()

    def test_deadline_expirado_nao_inicia_nova_tentativa(self):
        self._set_deadline(time.monotonic() - 1)
        operation = Mock()

        with self.assertRaises(rag.RequestDeadlineExceeded):
            rag._retry_on_transient(operation, max_retries=2, backoff=0)

        operation.assert_not_called()


class TestTransientRetry(unittest.TestCase):
    @staticmethod
    def _http_error(status_code: int, **headers) -> httpx.HTTPStatusError:
        request = httpx.Request("POST", "https://provider.invalid/v1/generate")
        response = httpx.Response(status_code, request=request, headers=headers)
        return httpx.HTTPStatusError("provider error", request=request, response=response)

    def setUp(self):
        self.token = rag._request_deadline.set(time.monotonic() + 60)

    def tearDown(self):
        rag._request_deadline.reset(self.token)

    def test_retry_after_e_respeitado_em_falha_transitoria(self):
        operation = Mock(side_effect=[self._http_error(429, **{"Retry-After": "2"}), "ok"])

        with patch("rag._time.sleep") as sleep:
            result = rag._retry_on_transient(operation, max_retries=2, backoff=0.1)

        self.assertEqual(result, "ok")
        self.assertEqual(operation.call_count, 2)
        sleep.assert_called_once_with(2.0)

    def test_erro_de_autenticacao_nao_e_repetido(self):
        operation = Mock(side_effect=self._http_error(401))

        with patch("rag._time.sleep") as sleep:
            with self.assertRaises(httpx.HTTPStatusError):
                rag._retry_on_transient(operation, max_retries=3, backoff=0)

        operation.assert_called_once_with()
        sleep.assert_not_called()

    def test_retry_e_pulado_quando_retry_after_nao_cabe_no_deadline(self):
        rag._request_deadline.reset(self.token)
        self.token = rag._request_deadline.set(time.monotonic() + 0.1)
        operation = Mock(side_effect=self._http_error(503, **{"Retry-After": "5"}))

        with patch("rag._time.sleep") as sleep:
            with self.assertRaises(httpx.HTTPStatusError):
                rag._retry_on_transient(operation, max_retries=2, backoff=0)

        operation.assert_called_once_with()
        sleep.assert_not_called()
