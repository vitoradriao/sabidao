import threading
import time
import unittest
from unittest.mock import Mock, patch

import httpx

import config
import jev
import rag


class _Transport:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def post(self, url, *, headers, json, timeout):
        self.calls.append(
            {"url": url, "headers": dict(headers), "json": json, "timeout": timeout}
        )
        if self.error:
            raise self.error
        return self.response


def _response(payload, status_code=200, headers=None):
    return httpx.Response(
        status_code,
        json=payload,
        headers=headers,
        request=httpx.Request("POST", jev.ENDPOINT),
    )


class _JsonResponse:
    def __init__(self, payload, status_code=200, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


class TestTypeSafeClient(unittest.TestCase):
    def setUp(self):
        jev.reset_shared_executor_for_tests()
        self.config_patch = patch.multiple(
            config,
            JEV_MODEL="jev-1.13.0",
            JEV_MAX_CONCURRENCY=2,
            JEV_REQUEST_TIMEOUT_SECONDS=0.2,
            JEV_STAGE_TIMEOUT_SECONDS=0.5,
            JEV_MIN_REMAINING_SECONDS=0.0,
        )
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.addCleanup(jev.reset_shared_executor_for_tests)

    def test_importar_e_instanciar_nao_cria_cliente_http(self):
        with patch("httpx.Client") as client_class:
            client = jev.TypeSafeClient()
        client_class.assert_not_called()
        client.close()

    def test_noul_e_choice_validos_preservam_uso_e_custo(self):
        response = _response(
            {
                "model": "jev-1.13.0",
                "answers": {
                    "relevant": {"type": "noul", "noul": 0.75},
                    "route": {
                        "type": "choice",
                        "choice": "a",
                        "probabilities": {"a": 0.8, "b": 0.2},
                        "confidence": 0.7,
                    },
                },
                "usage": {"input_tokens": 100, "output_tokens": 12},
            }
        )
        transport = _Transport(response)
        result = jev.TypeSafeClient(transport=transport, api_key="secret").decide(
            state="estado",
            questions={
                "relevant": {"type": "noul", "instructions": "E relevante?"},
                "route": {
                    "type": "choice",
                    "instructions": "Qual rota?",
                    "criteria": {"a": "A", "b": "B"},
                },
            },
            stage="rerank",
            request_id="request-1",
            candidate_id="candidate-1",
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.answers["relevant"], {"type": "noul", "noul": 0.75})
        self.assertNotIn("confidence", result.answers["relevant"])
        self.assertEqual(result.usage, {"input_tokens": 100, "output_tokens": 12})
        self.assertEqual(result.estimated_cost_usd, 0.0000042)
        self.assertEqual(result.cost_status, "estimated")
        self.assertEqual(result.attempt, 1)
        self.assertEqual(transport.calls[0]["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(transport.calls[0]["json"]["model"], "jev-1.13.0")

    def test_uso_de_saida_ausente_nao_impede_custo_de_entrada(self):
        transport = _Transport(
            _response(
                {
                    "model": "jev-1.13.0",
                    "answers": {"q": {"type": "noul", "noul": 0.5}},
                    "usage": {"input_tokens": 10},
                }
            )
        )
        result = jev.TypeSafeClient(transport=transport, api_key="secret").decide(
            state="estado",
            questions={"q": {"type": "noul", "instructions": "Q"}},
            stage="gate",
        )
        self.assertEqual(result.estimated_cost_usd, 0.00000042)

    def test_respostas_invalidas_nao_viram_decisao(self):
        cases = [
            {"model": "jev-other", "answers": {"q": {"type": "noul", "noul": 0.5}}},
            {"model": "jev-1.13.0", "answers": {}},
            {"model": "jev-1.13.0", "answers": {"q": {"type": "noul", "noul": float("nan")}}},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                transport = _Transport(_JsonResponse(payload))
                result = jev.TypeSafeClient(transport=transport, api_key="secret").decide(
                    state="estado",
                    questions={"q": {"type": "noul", "instructions": "Q"}},
                    stage="gate",
                )
                self.assertEqual(result.status, "invalid_response")
                self.assertEqual(result.answers, {})

    def test_http_erros_nao_repetem_a_chamada(self):
        for code, expected in (
            (401, "auth_error"),
            (403, "auth_error"),
            (429, "rate_limited"),
            (500, "provider_error"),
            (529, "provider_error"),
        ):
            with self.subTest(code=code):
                response = _response(
                    {"secret": "nao registrar"},
                    code,
                    headers={"Retry-After": "12"} if code == 429 else None,
                )
                transport = _Transport(response)
                result = jev.TypeSafeClient(transport=transport, api_key="secret").decide(
                    state="estado",
                    questions={"q": {"type": "noul", "instructions": "Q"}},
                    stage="gate",
                )
                self.assertEqual(result.status, expected)
                self.assertEqual(len(transport.calls), 1)
                self.assertNotIn("nao registrar", str(result))
                self.assertEqual(result.retry_after_seconds, 12.0 if code == 429 else None)

    def test_modelo_alias_e_configuracao_invalida_sao_rejeitados(self):
        with patch.object(config, "JEV_MODEL", "jev-latest"):
            with self.assertRaises(jev.JevConfigurationError):
                jev.TypeSafeClient(transport=_Transport(), api_key="secret").decide(
                    state="estado",
                    questions={"q": {"type": "noul", "instructions": "Q"}},
                    stage="gate",
                )

    def test_deadline_vencido_nao_inicia_chamada(self):
        transport = _Transport(_response({}))
        result = jev.TypeSafeClient(transport=transport, api_key="secret").decide(
            state="estado",
            questions={"q": {"type": "noul", "instructions": "Q"}},
            stage="gate",
            deadline=time.monotonic() - 1,
        )
        self.assertEqual(result.status, "budget_exhausted")
        self.assertEqual(transport.calls, [])

    def test_slot_global_permanece_ocupado_apos_timeout(self):
        started = threading.Event()
        release = threading.Event()

        class BlockingTransport(_Transport):
            def post(self, *args, **kwargs):
                self.calls.append(kwargs)
                started.set()
                release.wait(timeout=2)
                return _response(
                    {
                        "model": "jev-1.13.0",
                        "answers": {"q": {"type": "noul", "noul": 0.5}},
                    }
                )

        with patch.object(config, "JEV_MAX_CONCURRENCY", 1), patch.object(
            config, "JEV_REQUEST_TIMEOUT_SECONDS", 0.03
        ):
            transport = BlockingTransport()
            first_result = []

            def first_call():
                first_result.append(
                    jev.TypeSafeClient(transport=transport, api_key="secret").decide(
                        state="estado",
                        questions={"q": {"type": "noul", "instructions": "Q"}},
                        stage="gate",
                    )
                )

            thread = threading.Thread(target=first_call)
            thread.start()
            self.assertTrue(started.wait(1))
            second = jev.TypeSafeClient(transport=_Transport(), api_key="secret").decide(
                state="estado",
                questions={"q": {"type": "noul", "instructions": "Q"}},
                stage="gate",
            )
            self.assertEqual(second.status, "capacity_exhausted")
            time.sleep(0.08)
            release.set()
            thread.join(timeout=1)
            self.assertFalse(thread.is_alive())
            self.assertEqual(first_result[0].status, "timeout")

    def test_trace_jev_nao_confunde_candidato_com_retry(self):
        result = jev.DecisionResult(
            status="ok",
            model_requested="jev-1.13.0",
            model_effective="jev-1.13.0",
            answers={},
            usage={"input_tokens": 1, "output_tokens": None},
            latency_ms=1,
            error_code=None,
            request_id="request-1",
            call_id="call-1",
            candidate_id="candidate-1",
            estimated_cost_usd=0.000000042,
            cost_status="estimated",
        )
        calls = []
        rag._record_jev_decision(calls, result=result, stage="rerank")
        rag._record_jev_decision(
            calls,
            result=jev.DecisionResult(
                **{**result.__dict__, "call_id": "call-2", "candidate_id": "candidate-2"}
            ),
            stage="rerank",
        )
        self.assertEqual([call["attempt"] for call in calls], [1, 1])
        self.assertEqual([call["candidate_id"] for call in calls], ["candidate-1", "candidate-2"])


if __name__ == "__main__":
    unittest.main()
