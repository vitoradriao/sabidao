"""Cliente HTTP pequeno e opcional para decisoes estruturadas do TypeSafe.

Este modulo nao cria clientes HTTP, workers ou requisicoes ao ser importado. A
politica de uso (quando chamar, qual pergunta enviar e como interpretar a
decisao) pertence aos consumidores; aqui ficam somente transporte, contrato,
limites e telemetria operacional.
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
import math
import re
import threading
import time
from typing import Any, Callable, Mapping
import uuid

import httpx

import config


ENDPOINT = "https://api.typesafe.ai/v1/systemone"
CONTRACT_VERSION = "typesafe-systemone-v1"
PRICING_VERSION = "typesafe-2026-09-21"
INPUT_PRICE_USD_PER_MILLION = 0.042
ALLOWED_STATUSES = frozenset(
    {
        "ok",
        "timeout",
        "rate_limited",
        "auth_error",
        "provider_error",
        "invalid_response",
        "budget_exhausted",
        "capacity_exhausted",
    }
)
_VALID_QUESTION_TYPES = frozenset({"noul", "choice"})
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_VERSIONED_MODEL_RE = re.compile(r"^jev-\d+\.\d+\.\d+$")


class JevConfigurationError(EnvironmentError):
    """Configuracao ausente ou invalida quando uma decisao foi solicitada."""


@dataclass(frozen=True)
class DecisionResult:
    """Resultado validado de uma chamada, sem texto bruto do provider."""

    status: str
    model_requested: str
    model_effective: str | None
    answers: dict[str, dict[str, Any]]
    usage: dict[str, int | None]
    latency_ms: int
    error_code: str | None
    request_id: str
    call_id: str
    candidate_id: str | None = None
    attempt: int = 1
    completion_status: str = "completed"
    late_completion: bool = False
    estimated_cost_usd: float | None = None
    cost_status: str = "usage_unavailable"
    pricing_version: str = PRICING_VERSION
    retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.status not in ALLOWED_STATUSES:
            raise ValueError("status de decisao desconhecido")
        if self.attempt != 1:
            raise ValueError("o cliente TypeSafe nao executa retries automaticos")

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    def to_trace(self, *, stage: str, provider: str = "typesafe") -> dict[str, Any]:
        """Converte o resultado em um registro seguro para ``model_calls``."""
        return {
            "provider": provider,
            "stage": stage,
            "request_id": self.request_id,
            "call_id": self.call_id,
            "candidate_id": self.candidate_id,
            "attempt": self.attempt,
            "requested_model": self.model_requested,
            "model": self.model_effective or self.model_requested,
            "status": self.status,
            "latency_ms": self.latency_ms,
            "usage": self.usage,
            "estimated_cost_usd": self.estimated_cost_usd,
            "cost_status": self.cost_status,
            "pricing_version": self.pricing_version,
            "retry_after_seconds": self.retry_after_seconds,
            "error_code": self.error_code,
            "completion_status": self.completion_status,
            "late_completion": self.late_completion,
        }


@dataclass(frozen=True)
class _ValidatedConfig:
    api_key: str
    model: str
    max_concurrency: int
    request_timeout_seconds: float
    stage_timeout_seconds: float
    min_remaining_seconds: float


def _finite_number(value: Any, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} deve ser numerico")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} deve ser finito")
    return number


def _identifier(value: str | None, *, name: str, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{name} deve ser um identificador opaco valido")
    return value


def _usage(payload: Any) -> dict[str, int | None]:
    if payload is None:
        return {"input_tokens": None, "output_tokens": None}
    if not isinstance(payload, Mapping):
        raise ValueError("usage invalido")
    result: dict[str, int | None] = {}
    for field in ("input_tokens", "output_tokens"):
        value = payload.get(field)
        if value is None:
            result[field] = None
        elif isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"usage.{field} invalido")
        else:
            result[field] = value
    return result


def _estimate_cost(usage: dict[str, int | None]) -> tuple[float | None, str]:
    input_tokens = usage.get("input_tokens")
    if input_tokens is None:
        return None, "usage_unavailable"
    return round(input_tokens * INPUT_PRICE_USD_PER_MILLION / 1_000_000, 12), "estimated"


def _validate_questions(questions: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(questions, Mapping) or not questions:
        raise ValueError("questions deve ser um mapa nao vazio")
    validated: dict[str, dict[str, Any]] = {}
    for raw_id, question in questions.items():
        if not isinstance(raw_id, str):
            raise ValueError("question_id deve ser texto")
        question_id = _identifier(raw_id, name="question_id", required=True)
        if not isinstance(question, Mapping):
            raise ValueError("cada question deve ser um objeto")
        question_type = question.get("type")
        if question_type not in _VALID_QUESTION_TYPES:
            raise ValueError("type de question desconhecido")
        if "instructions" not in question:
            raise ValueError("question sem instructions")
        if question_type == "choice":
            criteria = question.get("criteria")
            if not isinstance(criteria, Mapping) or not criteria:
                raise ValueError("choice exige criteria nao vazio")
            labels = list(criteria)
            if any(not isinstance(label, str) or not label for label in labels):
                raise ValueError("label de choice invalido")
        elif "criteria" in question and question["criteria"] is not None and not isinstance(
            question["criteria"], Mapping
        ):
            raise ValueError("criteria de noul invalido")
        validated[question_id] = dict(question)
    return validated


def _validate_state(state: Any) -> None:
    if not isinstance(state, (str, Mapping, list, tuple)) or isinstance(state, bool):
        raise ValueError("state deve ser texto, objeto ou lista")


def _validate_config(*, api_key: str | None = None) -> _ValidatedConfig:
    key = api_key if api_key is not None else config.TYPESAFE_API_KEY
    if not isinstance(key, str) or not key.strip():
        raise JevConfigurationError("TYPESAFE_API_KEY obrigatoria quando Jev esta ativo")
    model = str(config.JEV_MODEL or "").strip()
    if not _VERSIONED_MODEL_RE.fullmatch(model):
        raise JevConfigurationError(
            "JEV_MODEL deve ser um identificador Jev versionado; aliases latest/preview nao sao aceitos"
        )
    try:
        if isinstance(config.JEV_MAX_CONCURRENCY, bool) or not isinstance(
            config.JEV_MAX_CONCURRENCY, int
        ):
            raise ValueError("concurrency nao inteiro")
        max_concurrency = config.JEV_MAX_CONCURRENCY
        request_timeout = float(config.JEV_REQUEST_TIMEOUT_SECONDS)
        stage_timeout = float(config.JEV_STAGE_TIMEOUT_SECONDS)
        min_remaining = float(config.JEV_MIN_REMAINING_SECONDS)
    except (TypeError, ValueError) as exc:
        raise JevConfigurationError("configuracao numerica Jev invalida") from exc
    if not 1 <= max_concurrency <= 8:
        raise JevConfigurationError("JEV_MAX_CONCURRENCY deve estar entre 1 e 8")
    if not math.isfinite(request_timeout) or request_timeout <= 0:
        raise JevConfigurationError("JEV_REQUEST_TIMEOUT_SECONDS deve ser finito e maior que zero")
    if not math.isfinite(stage_timeout) or stage_timeout <= 0:
        raise JevConfigurationError("JEV_STAGE_TIMEOUT_SECONDS deve ser finito e maior que zero")
    if not math.isfinite(min_remaining) or min_remaining < 0:
        raise JevConfigurationError("JEV_MIN_REMAINING_SECONDS deve ser finito e nao negativo")
    return _ValidatedConfig(
        api_key=key.strip(),
        model=model,
        max_concurrency=max_concurrency,
        request_timeout_seconds=request_timeout,
        stage_timeout_seconds=stage_timeout,
        min_remaining_seconds=min_remaining,
    )


class _SharedPool:
    def __init__(self, max_workers: int):
        self.max_workers = max_workers
        self._slots = threading.BoundedSemaphore(max_workers)
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="typesafe-jev",
        )
        self._lock = threading.Lock()
        self._active = 0
        self._closed = False

    @property
    def active_count(self) -> int:
        with self._lock:
            return self._active

    def submit(self, function: Callable[[], Any]) -> Future[Any] | None:
        if self._closed or not self._slots.acquire(blocking=False):
            return None
        with self._lock:
            if self._closed:
                self._slots.release()
                return None
            self._active += 1

        def run() -> Any:
            try:
                return function()
            finally:
                with self._lock:
                    self._active -= 1
                self._slots.release()

        try:
            return self._executor.submit(run)
        except Exception:
            with self._lock:
                self._active -= 1
            self._slots.release()
            raise

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)


_POOL_LOCK = threading.Lock()
_SHARED_POOL: _SharedPool | None = None


def _shared_pool(max_workers: int) -> _SharedPool:
    global _SHARED_POOL
    with _POOL_LOCK:
        if _SHARED_POOL is None:
            _SHARED_POOL = _SharedPool(max_workers)
        elif _SHARED_POOL.max_workers != max_workers and _SHARED_POOL.active_count == 0:
            _SHARED_POOL.shutdown()
            _SHARED_POOL = _SharedPool(max_workers)
        return _SHARED_POOL


def shutdown_shared_executor() -> None:
    """Encerra o executor global sem esperar trabalhos HTTP bloqueados."""
    global _SHARED_POOL
    with _POOL_LOCK:
        pool, _SHARED_POOL = _SHARED_POOL, None
    if pool is not None:
        pool.shutdown()


def reset_shared_executor_for_tests() -> None:
    shutdown_shared_executor()


class _HttpxTransport:
    def __init__(self):
        self._client: httpx.Client | None = None
        self._lock = threading.Lock()

    def post(self, url: str, *, headers: Mapping[str, str], json: Mapping[str, Any], timeout: float):
        with self._lock:
            if self._client is None:
                self._client = httpx.Client()
            client = self._client
        return client.post(url, headers=dict(headers), json=dict(json), timeout=timeout)

    def close(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            client.close()


def _status_for_http(status_code: int) -> tuple[str, str]:
    if status_code in {401, 403}:
        return "auth_error", f"http_{status_code}"
    if status_code == 429:
        return "rate_limited", "http_429"
    if status_code == 529 or 500 <= status_code <= 599:
        return "provider_error", f"http_{status_code}"
    return "invalid_response", f"http_{status_code}"


def _sanitize_retry_after(headers: Any) -> float | None:
    """Aceita somente um atraso numerico limitado, sem refletir headers brutos."""
    try:
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if raw is None:
            return None
        value = float(str(raw).strip())
    except (AttributeError, TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return min(value, 3600.0)


def _validated_answers(
    answers: Any,
    questions: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not isinstance(answers, Mapping) or set(answers) != set(questions):
        raise ValueError("answers nao corresponde aos IDs esperados")
    validated: dict[str, dict[str, Any]] = {}
    for question_id, question in questions.items():
        answer = answers.get(question_id)
        if not isinstance(answer, Mapping) or answer.get("type") != question["type"]:
            raise ValueError("tipo de answer divergente")
        if question["type"] == "noul":
            value = _finite_number(answer.get("noul"), name="noul")
            if not 0 <= value <= 1:
                raise ValueError("noul fora do intervalo")
            validated[question_id] = {"type": "noul", "noul": value}
            continue

        criteria = question["criteria"]
        choice = answer.get("choice")
        if not isinstance(choice, str) or choice not in criteria:
            raise ValueError("choice desconhecida")
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, Mapping) or set(probabilities) != set(criteria):
            raise ValueError("probabilities nao corresponde aos labels")
        checked_probabilities: dict[str, float] = {}
        total = 0.0
        for label in criteria:
            probability = _finite_number(probabilities.get(label), name="probability")
            if not 0 <= probability <= 1:
                raise ValueError("probability fora do intervalo")
            checked_probabilities[label] = probability
            total += probability
        if not math.isclose(total, 1.0, rel_tol=1e-6, abs_tol=1e-6):
            raise ValueError("probabilities nao somam 1")
        confidence = _finite_number(answer.get("confidence"), name="confidence")
        if not 0 <= confidence <= 1:
            raise ValueError("confidence fora do intervalo")
        validated[question_id] = {
            "type": "choice",
            "choice": choice,
            "probabilities": checked_probabilities,
            "confidence": confidence,
        }
    return validated


class TypeSafeClient:
    """Transporte TypeSafe sem retry automatico e com limite por processo."""

    def __init__(
        self,
        *,
        transport: Any | None = None,
        api_key: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._transport = transport
        self._api_key = api_key
        self._clock = clock
        self._late_completions: dict[str, dict[str, Any]] = {}
        self._late_lock = threading.Lock()
        self._owned_transport = transport is None

    @property
    def late_completions(self) -> dict[str, dict[str, Any]]:
        with self._late_lock:
            return {call_id: dict(value) for call_id, value in self._late_completions.items()}

    def _make_result(
        self,
        *,
        status: str,
        cfg: _ValidatedConfig,
        request_id: str,
        call_id: str,
        candidate_id: str | None,
        latency_ms: int,
        error_code: str | None = None,
        model_effective: str | None = None,
        answers: dict[str, dict[str, Any]] | None = None,
        usage: dict[str, int | None] | None = None,
        completion_status: str = "completed",
        late_completion: bool = False,
        retry_after_seconds: float | None = None,
    ) -> DecisionResult:
        safe_usage = usage or {"input_tokens": None, "output_tokens": None}
        cost, cost_status = _estimate_cost(safe_usage) if status == "ok" else (None, "usage_unavailable")
        return DecisionResult(
            status=status,
            model_requested=cfg.model,
            model_effective=model_effective,
            answers=answers or {},
            usage=safe_usage,
            latency_ms=max(0, int(latency_ms)),
            error_code=error_code,
            request_id=request_id,
            call_id=call_id,
            candidate_id=candidate_id,
            completion_status=completion_status,
            late_completion=late_completion,
            estimated_cost_usd=cost,
            cost_status=cost_status,
            retry_after_seconds=retry_after_seconds,
        )

    def _request(
        self,
        *,
        payload: dict[str, Any],
        cfg: _ValidatedConfig,
        request_id: str,
        call_id: str,
        candidate_id: str | None,
        timeout_seconds: float,
        worker_deadline: float,
        started_at: float,
        questions: dict[str, dict[str, Any]],
    ) -> DecisionResult:
        if self._clock() >= worker_deadline:
            return self._make_result(
                status="budget_exhausted",
                cfg=cfg,
                request_id=request_id,
                call_id=call_id,
                candidate_id=candidate_id,
                latency_ms=(self._clock() - started_at) * 1000,
                error_code="deadline_expired",
            )
        transport = self._transport
        if transport is None:
            transport = _HttpxTransport()
            self._transport = transport
        try:
            headers = {
                "Authorization": f"Bearer {cfg.api_key}",
                "Content-Type": "application/json",
            }
            if isinstance(transport, httpx.BaseTransport):
                with httpx.Client(transport=transport) as client:
                    response = client.post(
                        ENDPOINT,
                        headers=headers,
                        json=payload,
                        timeout=timeout_seconds,
                    )
            else:
                response = transport.post(
                    ENDPOINT,
                    headers=headers,
                    json=payload,
                    timeout=timeout_seconds,
                )
        except (httpx.TimeoutException, TimeoutError):
            return self._make_result(
                status="timeout",
                cfg=cfg,
                request_id=request_id,
                call_id=call_id,
                candidate_id=candidate_id,
                latency_ms=(self._clock() - started_at) * 1000,
                error_code="timeout",
            )
        except httpx.RequestError:
            return self._make_result(
                status="provider_error",
                cfg=cfg,
                request_id=request_id,
                call_id=call_id,
                candidate_id=candidate_id,
                latency_ms=(self._clock() - started_at) * 1000,
                error_code="transport_error",
            )
        except Exception:
            return self._make_result(
                status="provider_error",
                cfg=cfg,
                request_id=request_id,
                call_id=call_id,
                candidate_id=candidate_id,
                latency_ms=(self._clock() - started_at) * 1000,
                error_code="transport_error",
            )

        status_code = int(getattr(response, "status_code", 0) or 0)
        if status_code != 200:
            status, error_code = _status_for_http(status_code)
            return self._make_result(
                status=status,
                cfg=cfg,
                request_id=request_id,
                call_id=call_id,
                candidate_id=candidate_id,
                latency_ms=(self._clock() - started_at) * 1000,
                error_code=error_code,
                retry_after_seconds=_sanitize_retry_after(getattr(response, "headers", {})),
            )
        try:
            body = response.json()
            if not isinstance(body, Mapping) or body.get("model") != cfg.model:
                raise ValueError("model divergente")
            answers = _validated_answers(body.get("answers"), questions)
            usage = _usage(body.get("usage"))
        except (ValueError, TypeError, KeyError):
            return self._make_result(
                status="invalid_response",
                cfg=cfg,
                request_id=request_id,
                call_id=call_id,
                candidate_id=candidate_id,
                latency_ms=(self._clock() - started_at) * 1000,
                error_code="invalid_response",
            )
        return self._make_result(
            status="ok",
            cfg=cfg,
            request_id=request_id,
            call_id=call_id,
            candidate_id=candidate_id,
            latency_ms=(self._clock() - started_at) * 1000,
            model_effective=cfg.model,
            answers=answers,
            usage=usage,
        )

    def decide(
        self,
        state: Any,
        questions: Mapping[str, Any],
        *,
        stage: str,
        request_id: str | None = None,
        candidate_id: str | None = None,
        deadline: float | None = None,
        stage_budget_seconds: float | None = None,
    ) -> DecisionResult:
        """Avalia perguntas sem bloquear alem do orçamento informado."""
        cfg = _validate_config(api_key=self._api_key)
        _validate_state(state)
        validated_questions = _validate_questions(questions)
        _identifier(stage, name="stage", required=True)
        request_id = _identifier(request_id, name="request_id") or uuid.uuid4().hex
        candidate_id = _identifier(candidate_id, name="candidate_id")
        now = self._clock()
        if deadline is not None and not math.isfinite(float(deadline)):
            raise ValueError("deadline deve ser finito")
        if stage_budget_seconds is not None:
            stage_budget_seconds = _finite_number(
                stage_budget_seconds,
                name="stage_budget_seconds",
            )
            if stage_budget_seconds <= 0:
                return self._make_result(
                    status="budget_exhausted",
                    cfg=cfg,
                    request_id=request_id,
                    call_id=uuid.uuid4().hex,
                    candidate_id=candidate_id,
                    latency_ms=0,
                    error_code="stage_budget_exhausted",
                )
        global_available = float("inf") if deadline is None else float(deadline) - now
        if global_available <= cfg.min_remaining_seconds:
            return self._make_result(
                status="budget_exhausted",
                cfg=cfg,
                request_id=request_id,
                call_id=uuid.uuid4().hex,
                candidate_id=candidate_id,
                latency_ms=0,
                error_code="deadline_reserve",
            )
        available = global_available - (
            cfg.min_remaining_seconds if deadline is not None else 0.0
        )
        if stage_budget_seconds is not None:
            available = min(available, stage_budget_seconds)
        timeout_seconds = min(cfg.request_timeout_seconds, cfg.stage_timeout_seconds, available)
        if timeout_seconds <= 0:
            return self._make_result(
                status="budget_exhausted",
                cfg=cfg,
                request_id=request_id,
                call_id=uuid.uuid4().hex,
                candidate_id=candidate_id,
                latency_ms=0,
                error_code="deadline_expired",
            )

        call_id = uuid.uuid4().hex
        payload = {"state": state, "model": cfg.model, "questions": validated_questions}
        pool = _shared_pool(cfg.max_concurrency)
        started_at = self._clock()
        future = pool.submit(
            lambda: self._request(
                payload=payload,
                cfg=cfg,
                request_id=request_id,
                call_id=call_id,
                candidate_id=candidate_id,
                timeout_seconds=timeout_seconds,
                worker_deadline=started_at + timeout_seconds,
                started_at=started_at,
                questions=validated_questions,
            )
        )
        if future is None:
            return self._make_result(
                status="capacity_exhausted",
                cfg=cfg,
                request_id=request_id,
                call_id=call_id,
                candidate_id=candidate_id,
                latency_ms=(self._clock() - started_at) * 1000,
                error_code="capacity_exhausted",
            )
        wait_timeout = timeout_seconds
        if deadline is not None:
            wait_timeout = min(wait_timeout, max(0.0, float(deadline) - self._clock()))
        try:
            return future.result(timeout=wait_timeout)
        except FutureTimeoutError:
            with self._late_lock:
                self._late_completions[call_id] = {
                    "status": "pending",
                    "request_id": request_id,
                    "candidate_id": candidate_id,
                }

            def remember_completion(done: Future[Any]) -> None:
                with self._late_lock:
                    item = self._late_completions.get(call_id)
                    if item is None:
                        return
                    try:
                        late_result = done.result()
                    except Exception:
                        item.update({"status": "provider_error", "error_code": "transport_error"})
                    else:
                        item.update(
                            {
                                "status": getattr(late_result, "status", "provider_error"),
                                "completion_status": "completed_late",
                            }
                        )

            future.add_done_callback(remember_completion)
            return self._make_result(
                status="timeout",
                cfg=cfg,
                request_id=request_id,
                call_id=call_id,
                candidate_id=candidate_id,
                latency_ms=(self._clock() - started_at) * 1000,
                error_code="timeout",
                completion_status="pending",
            )
        except Exception:
            return self._make_result(
                status="provider_error",
                cfg=cfg,
                request_id=request_id,
                call_id=call_id,
                candidate_id=candidate_id,
                latency_ms=(self._clock() - started_at) * 1000,
                error_code="transport_error",
            )

    evaluate = decide
    request = decide

    def close(self) -> None:
        if self._owned_transport and isinstance(self._transport, _HttpxTransport):
            self._transport.close()


def validate_jev_config(*, active: bool = False) -> None:
    """Valida Jev apenas quando um consumidor realmente o habilita."""
    if active:
        _validate_config()


shutdown = shutdown_shared_executor
