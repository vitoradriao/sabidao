"""Reserva conservadora por chamada HTTP para ensaios locais autorizados.

O livro de chamadas contém somente metadados e custos estimados. Credenciais,
URLs completas, prompts e respostas nunca são persistidos. Não altera o bot:
o responsável instala o wrapper somente no processo do experimento.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import threading
import time
from typing import Callable


class ExperimentBudgetExceeded(RuntimeError):
    """Nenhuma chamada nova pode ser admitida dentro do teto do ensaio."""


class ExperimentBudget:
    def __init__(self, path: Path, *, limit_usd: float, prices: dict):
        if not math.isfinite(limit_usd) or limit_usd <= 0:
            raise ValueError("O orçamento deve ser finito e positivo.")
        self.path = Path(path)
        self._lock = threading.Lock()
        for rates in prices.values():
            for field in ("input", "output"):
                value = rates.get(field)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    raise ValueError("Tarifa inválida no perfil do experimento.")
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
            if self.data["limit_usd"] != limit_usd or self.data["prices"] != prices:
                raise ValueError("Não altere orçamento/tarifas de um livro já iniciado.")
        else:
            self.data = {"schema_version": 1, "limit_usd": limit_usd, "prices": prices, "calls": [], "blocked": False}
            self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.path)

    @property
    def committed_usd(self):
        return math.fsum(call["charged_upper_bound_usd"] for call in self.data["calls"])

    def _request_limits(self, request):
        payload = json.loads(request.content)
        host = request.url.host
        operation = request.url.path.rsplit("/", 1)[-1]
        if host == "api.deepseek.com" and operation == "completions":
            price_id = "deepseek:" + payload.get("model", "")
            output = payload.get("max_tokens") or payload.get("max_completion_tokens")
        elif host == "api.typesafe.ai" and operation == "systemone":
            price_id = "typesafe:" + payload.get("model", "")
            output = 0
        elif host == "generativelanguage.googleapis.com" and ":" in operation:
            model, action = operation.rsplit(":", 1)
            price_id = "gemini:" + model
            if action in {"embedContent", "batchEmbedContents"}:
                output = 0
            elif action == "generateContent":
                output = (payload.get("generationConfig") or {}).get("maxOutputTokens")
            else:
                raise ExperimentBudgetExceeded("Operação sem tarifa/reserva registrada.")
        else:
            raise ExperimentBudgetExceeded("Endpoint não registrado no experimento.")
        if price_id not in self.data["prices"] or type(output) is not int or output < 0:
            raise ExperimentBudgetExceeded("Modelo ou limite de saída não registrado.")
        # Um byte UTF-8 por token, mais overhead, é deliberadamente mais
        # conservador que o estimador de contexto bytes/2 usado pelo produto.
        input_bound = len(request.content) + 1024
        prices = self.data["prices"][price_id]
        reservation = (input_bound * prices["input"] + output * prices["output"]) / 1_000_000
        return price_id, input_bound, output, reservation

    def send(self, original_send: Callable, client, request, **kwargs):
        with self._lock:
            if self.data["blocked"]:
                raise ExperimentBudgetExceeded("Experimento interrompido pelo orçamento.")
            price_id, input_bound, output_bound, reservation = self._request_limits(request)
            if self.committed_usd + reservation > self.data["limit_usd"]:
                self.data["blocked"] = True
                self._save()
                raise ExperimentBudgetExceeded("Reserva da próxima chamada excede o orçamento.")
            call = {
                "call_id": f"http-{len(self.data['calls']) + 1}",
                "price_id": price_id,
                "input_upper_bound_tokens": input_bound,
                "output_upper_bound_tokens": output_bound,
                "charged_upper_bound_usd": reservation,
                "estimated_cost_usd": None,
                "status": "pending",
            }
            self.data["calls"].append(call)
            self._save()
        started = time.monotonic()
        try:
            response = original_send(client, request, **kwargs)
            response.read()
        except Exception:
            with self._lock:
                call["status"] = "transport_error_usage_unknown"
                call["latency_ms"] = round((time.monotonic() - started) * 1000)
                self._save()
            raise
        with self._lock:
            call["http_status"] = response.status_code
            call["latency_ms"] = round((time.monotonic() - started) * 1000)
            call["status"] = "usage_unavailable_reservation_retained"
            if response.status_code == 200:
                try:
                    body = response.json()
                    if price_id.startswith("deepseek:"):
                        usage = body.get("usage") or {}
                        input_tokens = usage.get("prompt_tokens")
                        output_tokens = usage.get("completion_tokens")
                    elif price_id.startswith("typesafe:"):
                        usage = body.get("usage") or {}
                        input_tokens = usage.get("input_tokens")
                        output_tokens = 0
                    else:
                        usage = body.get("usageMetadata") or {}
                        input_tokens = usage.get("promptTokenCount")
                        total = usage.get("totalTokenCount")
                        output_tokens = max(0, total - input_tokens) if type(total) is int and type(input_tokens) is int else None
                    if all(type(value) is int and value >= 0 for value in (input_tokens, output_tokens)):
                        rates = self.data["prices"][price_id]
                        cost = (input_tokens * rates["input"] + output_tokens * rates["output"]) / 1_000_000
                        call.update(input_tokens=input_tokens, output_tokens=output_tokens, estimated_cost_usd=cost, charged_upper_bound_usd=cost, status="estimated_at_registered_ceiling_rates")
                        if input_tokens > input_bound or output_tokens > output_bound:
                            call["status"] = "token_bound_violated"
                            self.data["blocked"] = True
                except (ValueError, TypeError, AttributeError):
                    pass
            self._save()
        return response

    def summary(self):
        known = [call["estimated_cost_usd"] for call in self.data["calls"] if call["estimated_cost_usd"] is not None]
        return {
            "limit_usd": self.data["limit_usd"],
            "calls": len(self.data["calls"]),
            "known_estimated_cost_usd": math.fsum(known),
            "committed_upper_bound_usd": self.committed_usd,
            "usage_unknown_calls": len(self.data["calls"]) - len(known),
            "blocked": self.data["blocked"],
            "billing_verified": False,
        }
