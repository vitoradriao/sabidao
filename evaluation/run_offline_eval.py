"""
Runs offline end-to-end evaluation against the current RAG pipeline and stores results in Supabase.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any, Callable, Mapping

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
import jev
import rag
from bot_common import normalize_text


EVALUATOR_SCHEMA_VERSION = 7
METRIC_DEFINITIONS_VERSION = 2
COMPARISON_PROFILE_VERSION = 1
COMPARISON_SCHEMA_VERSION = 1
VARIANT_EXISTING = "existing"
VARIANT_JEV_RERANK = "jev_rerank"
VARIANT_JEV_RERANK_GATE = "jev_rerank+evidence_gate"
COMPARISON_VARIANTS = (
    VARIANT_EXISTING,
    VARIANT_JEV_RERANK,
    VARIANT_JEV_RERANK_GATE,
)
RETRIEVAL_STAGE_NAMES = (
    "sections",
    "candidate_pool",
    "post_rerank",
    "post_gate",
    "final_context",
)
COMPARISON_VIEWS = (
    "ranking_ablation_same_pool",
    "end_to_end_same_snapshot",
)


class VariantConfigurationError(ValueError):
    """A variante ou o perfil de comparação não respeita o contrato."""


class VariantUnavailableError(RuntimeError):
    """A variante foi solicitada antes de existir um caminho de execução ativo."""


_VARIANT_DEFINITIONS = {
    VARIANT_EXISTING: {
        "label": "A — existing",
        "available_without_provider": True,
        "requires": [],
    },
    VARIANT_JEV_RERANK: {
        "label": "B — jev_rerank",
        "available_without_provider": False,
        "requires": ["#91"],
    },
    VARIANT_JEV_RERANK_GATE: {
        "label": "C — jev_rerank+evidence_gate",
        "available_without_provider": False,
        "requires": ["#91", "#92"],
    },
}
METRIC_DEFINITIONS = {
    "behavior_match": (
        "Compara se a resposta ou abstencao ocorreu conforme expected_behavior."
    ),
    "factual_correctness": (
        "Exige que cada fato esperado tenha ao menos uma frase aceita presente na resposta; "
        "fica nao avaliada quando expected_facts nao foi informado."
    ),
    "retrieval_relevance": (
        "Verifica se ao menos uma evidencia de referencia foi recuperada pela fonte e, "
        "quando informados, pelos termos esperados."
    ),
    "recall_at_10": "Fracao das referencias distintas encontradas ate a posicao 10.",
    "recall_at_20": "Fracao das referencias distintas encontradas ate a posicao 20.",
    "evidence_discounted_coverage_at_10": (
        "Cobertura descontada das referencias distintas ate a posicao 10: para cada "
        "referencia encontrada, soma 1/log2(primeiro_rank+1), dividida pelo total "
        "de referencias. Nao e nDCG."
    ),
    "ndcg_at_10": (
        "nDCG convencional ate a posicao 10, calculado somente com qrels explicitos "
        "e estaveis por candidate_id no universo julgado declarado."
    ),
    "citation_validity": (
        "Verifica citacao de uma fonte de referencia sem erros de grounding; abstencoes e "
        "casos sem fonte de referencia nao sao avaliados."
    ),
    "intent_match": "Compara a intencao prevista com expected_intent.",
    "false_abstention": (
        "Marca abstencao indevida em caso cujo expected_behavior exige resposta exata."
    ),
    "false_absence_claim": (
        "Marca afirmacao explicita de ausencia em caso que exige resposta exata."
    ),
    "unsupported_claims": (
        "Proxy deterministico: detecta fatos proibidos e erros do validador de grounding; "
        "nao substitui revisao semantica humana."
    ),
}

_NEGATED_ABSENCE_RE = re.compile(r"\bnao (?:e|eh) inexistente\b")
_ABSENCE_CLAIM_RE = re.compile(
    r"\b(?:nao (?:existe|consta|ha|foi encontrad[oa]s?|esta documentad[oa]s?)|"
    r"(?:e|eh) inexistente)\b"
)
_OCCURRENCE_METRICS = frozenset(
    {"false_abstention", "false_absence_claim", "unsupported_claims"}
)
_CONTINUOUS_METRICS = frozenset(
    {
        "recall_at_10",
        "recall_at_20",
        "evidence_discounted_coverage_at_10",
        "ndcg_at_10",
    }
)

_CLARIFICATION_RE = re.compile(
    r"\b(?:pode|consegue|preciso|informe|confirme|especifique|qual|quais|onde|quando)\b"
)

_CONFIG_FIELDS = (
    "RAG_ENABLE_INTENT_ROUTING",
    "RAG_FILTER_BY_DOC_TYPE",
    "RAG_FILTER_BY_MODULE",
    "RAG_ENABLE_GLOBAL_CHALLENGER",
    "RAG_GLOBAL_CHALLENGER_COUNT",
    "RAG_GLOBAL_CHALLENGER_FETCH_LIMIT",
    "ANALYTICAL_CONTEXT_ENABLED",
    "SECTION_RETRIEVAL_ENABLED",
    "SECTION_MATCH_COUNT",
    "SECTION_FETCH_LIMIT",
    "MAX_CONTEXT_CHUNKS",
    "RAG_MAX_INPUT_TOKENS",
    "RAG_MAX_HISTORY_TOKENS",
    "RAG_MODEL_CONTEXT_TOKENS",
    "RAG_CONTEXT_MARGIN_TOKENS",
    "RAG_IMAGE_TOKEN_RESERVE",
    "CHUNK_FETCH_LIMIT",
    "MAX_CHUNKS_PER_SECTION",
    "MAX_CHUNKS_PER_DOCUMENT",
    "SIMILARITY_THRESHOLD",
    "SIMILARITY_FLOOR_FACTOR",
    "RAG_ENABLE_QUERY_REFORMULATION",
    "REFORMULATION_MODEL",
    "RAG_MIN_STRONG_SIMILARITY",
    "RAG_MIN_RETRIEVED_CHUNKS",
    "RAG_OPERATIONAL_SIMILARITY_MARGIN",
    "RAG_ENABLE_RERANKING",
    "RERANKER_MODEL",
    "RERANKER_MIN_TRIGGER_SIM",
    "RERANKER_MAX_TRIGGER_SIM",
    "RERANKER_MAX_CANDIDATES",
    "RAG_STRICT_ABSTAIN",
    "RAG_ENABLE_GROUNDING_VALIDATION",
    "RAG_REQUIRE_SOURCES_SECTION",
    "RAG_MAX_REGEN_ATTEMPTS",
    "RAG_FEEDBACK_TOP_K",
    "RAG_FEEDBACK_MIN_SIMILARITY",
    "RAG_ENABLE_BUSINESS_RULES",
    "BUSINESS_RULES_MAX_CHARS",
    "FULL_CONTEXT_ENABLED",
    "FULL_CONTEXT_MAX_CHARS",
    "FULL_CONTEXT_EXTENSIONS",
    "CONTEXTUAL_RETRIEVAL_ENABLED",
    "CONTEXTUAL_RETRIEVAL_MODEL",
    "CONTEXTUAL_RETRIEVAL_MAX_DOC_CHARS",
    "CONTEXTUAL_RETRIEVAL_MAX_TOKENS",
    "CONTEXTUAL_RETRIEVAL_BATCH_SIZE",
    "ASK_MAX_TOKENS",
    "OPENAI_MAX_OUTPUT_TOKENS",
    "GENERATION_MODEL_POLICY",
    "RAG_PROVIDER_MAX_RETRIES",
    "RAG_RETRY_BASE_SECONDS",
    "JEV_MODEL",
    "JEV_MAX_CONCURRENCY",
    "JEV_REQUEST_TIMEOUT_SECONDS",
    "JEV_STAGE_TIMEOUT_SECONDS",
    "JEV_MIN_REMAINING_SECONDS",
)

AnswerProvider = Callable[[str, dict[str, Any]], tuple[str, list[dict], dict[str, Any]]]
ComparisonProvider = Callable[..., tuple[str, list[dict], dict[str, Any]]]


def _comparison_selected_cases(
    dataset: list[dict[str, Any]],
    *,
    split: str,
    limit: int | None,
) -> list[dict[str, Any]]:
    selected = [
        case
        for case in dataset
        if split == "all" or str(case.get("split") or "development") == split
    ]
    return selected[:limit] if limit and limit > 0 else selected


def _metric_unavailable_reason(
    metric_name: str,
    result: dict[str, Any],
) -> str | None:
    details = (result.get("metric_details") or {}).get(metric_name)
    if isinstance(details, dict):
        reason = details.get("unavailable_reason") or details.get("reason")
        if reason:
            return str(reason)
    if result.get(metric_name) is None:
        return "metric_not_evaluated"
    return None


def _metric_observation(metric_name: str, result: dict[str, Any]) -> dict[str, Any]:
    value = result.get(metric_name)
    return {
        "value": value,
        "denominator": 1 if value is not None else 0,
        "unavailable_reason": _metric_unavailable_reason(metric_name, result),
        "metric_version": str(METRIC_DEFINITIONS_VERSION),
    }


def _invoke_comparison_provider(
    provider: ComparisonProvider | None,
    *,
    case: dict[str, Any],
    scope: dict[str, Any],
    variant_id: str,
    mode: str,
    candidate_pool: list[dict] | None,
) -> tuple[str, list[dict], dict[str, Any]]:
    if provider is None:
        raise VariantUnavailableError(f"provider ausente para variante: {variant_id}")
    question = str(case.get("question") or "").strip()
    try:
        import inspect

        parameters = inspect.signature(provider).parameters
        accepts_kwargs = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters.values()
        )
        if accepts_kwargs or {
            "variant_id",
            "mode",
            "candidate_pool",
        }.issubset(parameters):
            return provider(
                question,
                scope,
                variant_id=variant_id,
                mode=mode,
                candidate_pool=candidate_pool,
            )
        if len(parameters) >= 5:
            return provider(question, scope, variant_id, mode, candidate_pool)
        if len(parameters) >= 3:
            return provider(question, scope, variant_id)
        return provider(question, scope)
    except (TypeError, ValueError) as exc:
        raise VariantConfigurationError(
            f"provider inválido para variante {variant_id}: {type(exc).__name__}"
        ) from exc


def _load_dataset(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"Dataset file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, list):
        raise ValueError("Dataset must be a JSON array")
    cases = [item for item in payload if isinstance(item, dict) and str(item.get("question") or "").strip()]
    if not cases:
        raise ValueError("Dataset has no valid cases")
    return cases


def _load_json_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return payload


def _variant_definition(variant_id: str) -> dict[str, Any]:
    normalized = str(variant_id or "").strip()
    definition = _VARIANT_DEFINITIONS.get(normalized)
    if definition is None:
        raise VariantConfigurationError(
            f"Variante desconhecida: {normalized or '<vazia>'}. "
            f"Use uma de: {', '.join(COMPARISON_VARIANTS)}."
        )
    return {"variant_id": normalized, **definition}


def _comparison_profile(baseline_config: dict[str, Any] | None) -> dict[str, Any]:
    profile = (baseline_config or {}).get("comparison")
    if not isinstance(profile, dict):
        raise VariantConfigurationError(
            "baseline_config.comparison é obrigatório para uma comparação pareada."
        )
    if profile.get("profile_version") != COMPARISON_PROFILE_VERSION:
        raise VariantConfigurationError(
            "baseline_config.comparison.profile_version incompatível."
        )
    variants = profile.get("variants")
    if not isinstance(variants, list) or not variants:
        raise VariantConfigurationError(
            "baseline_config.comparison.variants deve ser uma lista não vazia."
        )
    normalized_variants = [str(variant_id).strip() for variant_id in variants]
    if len(set(normalized_variants)) != len(normalized_variants):
        raise VariantConfigurationError(
            "baseline_config.comparison.variants não pode repetir variantes."
        )
    for variant_id in normalized_variants:
        _variant_definition(variant_id)
    unavailable = profile.get("unavailable_variants") or {}
    if not isinstance(unavailable, dict):
        raise VariantConfigurationError(
            "baseline_config.comparison.unavailable_variants deve ser um objeto."
        )
    registered_variants = set(normalized_variants)
    for variant_id, definition in unavailable.items():
        normalized_id = str(variant_id).strip()
        _variant_definition(normalized_id)
        if normalized_id in registered_variants:
            raise VariantConfigurationError(
                f"Variante registrada como ativa e indisponível: {normalized_id}."
            )
        if not isinstance(definition, dict):
            raise VariantConfigurationError(
                f"Definição inválida para variante indisponível: {normalized_id}."
            )
        if definition.get("status") != "unavailable":
            raise VariantConfigurationError(
                f"Variante indisponível sem status explícito: {normalized_id}."
            )
        reason = definition.get("reason") or definition.get("unavailable_reason")
        if not isinstance(reason, str) or not reason.strip():
            raise VariantConfigurationError(
                f"Variante indisponível sem motivo: {normalized_id}."
            )
    views = profile.get("views")
    if not isinstance(views, list) or not views:
        raise VariantConfigurationError(
            "baseline_config.comparison.views deve ser uma lista não vazia."
        )
    unknown_views = [view for view in views if view not in COMPARISON_VIEWS]
    if unknown_views:
        raise VariantConfigurationError(
            f"Visões de comparação desconhecidas: {', '.join(map(str, unknown_views))}."
        )
    gate_declaration = unavailable.get(VARIANT_JEV_RERANK_GATE)
    if gate_declaration is None:
        raise VariantConfigurationError(
            "evidence_gate deve permanecer explicitamente indisponível no perfil."
        )
    controls = profile.get("controls")
    if not isinstance(controls, dict):
        raise VariantConfigurationError(
            "baseline_config.comparison.controls é obrigatório."
        )
    for field in (
        "same_dataset",
        "same_snapshot",
        "same_retrieval_pool_for_ranking_ablation",
        "same_evidence_window",
        "same_generation_and_embedding_configuration",
    ):
        if controls.get(field) is not True:
            raise VariantConfigurationError(
                f"Controle pareado incompatível ou ausente: {field}."
            )
    context_builder = controls.get("same_context_builder")
    evidence_window = controls.get("evidence_window")
    if not isinstance(context_builder, str) or not context_builder.strip():
        raise VariantConfigurationError(
            "controls.same_context_builder deve identificar o construtor congelado."
        )
    if not isinstance(evidence_window, dict):
        raise VariantConfigurationError(
            "controls.evidence_window deve ser um objeto."
        )
    if (
        evidence_window.get("builder") != context_builder
        or evidence_window.get("raw_text_persisted") is not False
    ):
        raise VariantConfigurationError(
            "controls.evidence_window não corresponde ao construtor ou expõe texto bruto."
        )
    return {
        **profile,
        "variants": normalized_variants,
        "unavailable_variants": {
            str(variant_id).strip(): dict(definition)
            for variant_id, definition in unavailable.items()
        },
    }


def _comparison_identifiers(
    profile: dict[str, Any],
    *,
    pair_id: str | None,
    snapshot_id: str | None,
    require_snapshot: bool,
) -> tuple[str, str | None]:
    resolved_pair_id = str(pair_id or profile.get("pair_id") or "").strip()
    resolved_snapshot_id = str(snapshot_id or profile.get("snapshot_id") or "").strip()
    if not resolved_pair_id:
        raise VariantConfigurationError(
            "A comparação pareada exige um pair_id estável."
        )
    if require_snapshot and not resolved_snapshot_id:
        raise VariantConfigurationError(
            "A comparação pareada exige snapshot_id; não presuma o snapshot do banco."
        )
    return resolved_pair_id, resolved_snapshot_id or None


def _validate_variant_execution(
    variant_id: str,
    *,
    answer_provider: AnswerProvider | None,
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    definition = _variant_definition(variant_id)
    unavailable = (profile or {}).get("unavailable_variants", {})
    if variant_id in unavailable:
        reason = unavailable[variant_id].get("reason") or unavailable[variant_id].get(
            "unavailable_reason"
        )
        raise VariantUnavailableError(
            f"A variante {variant_id} está indisponível: {reason}."
        )
    if not definition["available_without_provider"] and answer_provider is None:
        requirements = ", ".join(definition["requires"])
        raise VariantUnavailableError(
            f"A variante {variant_id} ainda não possui caminho ativo no rag.ask; "
            f"integre {requirements} ou forneça um provider fake explícito."
        )
    return definition


def _comparison_metadata(
    *,
    variant_id: str,
    pair_id: str | None,
    snapshot_id: str | None,
    view: str,
    profile: dict[str, Any] | None,
) -> dict[str, Any]:
    definition = _variant_definition(variant_id)
    controls = (profile or {}).get("controls") or {}
    return {
        "profile_version": (profile or {}).get(
            "profile_version", COMPARISON_PROFILE_VERSION
        ),
        "variant_id": variant_id,
        "variant_label": definition["label"],
        "pair_id": pair_id,
        "snapshot_id": snapshot_id,
        "view": view,
        "requested_variant": variant_id,
        "effective_variant": variant_id,
        "fallback_stage": None,
        "fallback_reason": None,
        "effective_reranker_provider": None,
        "effective_reranker_model": None,
        "evidence_window": controls.get("evidence_window")
        or controls.get("same_evidence_window"),
    }


def _is_abstained(answer: str, trace: dict[str, Any]) -> bool:
    if bool(trace.get("abstained")):
        return True
    return str(answer or "").strip().startswith(config.NO_ANSWER_PHRASE)


def _is_clarification(answer: str, trace: dict[str, Any]) -> bool:
    if trace.get("response_state") == "clarification_needed":
        return True
    normalized_answer = normalize_text(answer or "")
    return bool("?" in (answer or "") and _CLARIFICATION_RE.search(normalized_answer))


def _behavior_match(
    expected_behavior: str,
    abstained: bool,
    clarified: bool,
    answer: str,
) -> bool:
    expected = (expected_behavior or "exact_answer").strip().lower()
    if expected == "exact_answer":
        return not abstained and not clarified
    if expected == "no_answer":
        return abstained
    if expected == "clarify":
        return clarified
    if expected == "partial_abstain":
        # Accept either a strict abstain or a partial response that includes no-answer phrase.
        return abstained or (config.NO_ANSWER_PHRASE in (answer or ""))
    return not abstained and not clarified


def _false_abstention(expected_behavior: str, abstained: bool) -> bool | None:
    if (expected_behavior or "").strip().lower() != "exact_answer":
        return None
    return abstained


def _false_absence_claim(
    expected_behavior: str,
    abstained: bool,
    answer: str,
) -> bool | None:
    if (expected_behavior or "").strip().lower() != "exact_answer":
        return None
    if abstained:
        return False
    normalized_answer = normalize_text(answer or "")
    normalized_answer = _NEGATED_ABSENCE_RE.sub("", normalized_answer)
    return bool(_ABSENCE_CLAIM_RE.search(normalized_answer))


def _fact_specs(expected_facts: Any) -> list[dict[str, Any]]:
    if not isinstance(expected_facts, list):
        return []

    specs: list[dict[str, Any]] = []
    for index, item in enumerate(expected_facts, start=1):
        if isinstance(item, str) and item.strip():
            specs.append({"id": f"fact-{index}", "accepted_phrases": [item.strip()]})
            continue
        if not isinstance(item, dict):
            continue

        raw_phrases = item.get("accepted_phrases")
        if isinstance(raw_phrases, str):
            raw_phrases = [raw_phrases]
        if not isinstance(raw_phrases, list):
            raw_phrases = [item.get("text")] if item.get("text") else []
        phrases = [
            str(value).strip()
            for value in raw_phrases
            if value is not None and str(value).strip()
        ]
        if phrases:
            specs.append(
                {
                    "id": str(item.get("id") or f"fact-{index}"),
                    "accepted_phrases": phrases,
                }
            )
    return specs


def _factual_correctness(
    answer: str,
    abstained: bool,
    expected_facts: Any,
) -> tuple[bool | None, dict[str, Any]]:
    specs = _fact_specs(expected_facts)
    if not specs:
        return None, {"reason": "missing_expected_facts", "matched": [], "missing": []}

    if abstained:
        return False, {
            "reason": "answer_abstained",
            "matched": [],
            "missing": [spec["id"] for spec in specs],
        }

    normalized_answer = normalize_text(answer)
    matched: list[str] = []
    missing: list[str] = []
    for spec in specs:
        phrase_matches = [
            phrase
            for phrase in spec["accepted_phrases"]
            if normalize_text(phrase) in normalized_answer
        ]
        if phrase_matches:
            matched.append(spec["id"])
        else:
            missing.append(spec["id"])

    return not missing, {"matched": matched, "missing": missing}


def _forbidden_fact_matches(answer: str, forbidden_facts: Any) -> list[str]:
    specs = _fact_specs(forbidden_facts)
    normalized_answer = normalize_text(answer or "")
    return [
        spec["id"]
        for spec in specs
        if any(
            normalize_text(phrase) in normalized_answer
            for phrase in spec["accepted_phrases"]
        )
    ]


def _evidence_specs(reference_evidence: Any) -> list[dict[str, Any]]:
    if not isinstance(reference_evidence, list):
        return []

    specs: list[dict[str, Any]] = []
    for item in reference_evidence:
        if isinstance(item, str) and item.strip():
            specs.append({"source": item.strip(), "contains": []})
            continue
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "").strip()
        raw_terms = item.get("contains") or []
        if isinstance(raw_terms, str):
            raw_terms = [raw_terms]
        terms = [
            str(value).strip()
            for value in raw_terms
            if value is not None and str(value).strip()
        ]
        if source or terms:
            specs.append({"source": source, "contains": terms})
    return specs


def _source_matches(actual: Any, expected: Any) -> bool:
    actual_value = str(actual or "").strip().replace("\\", "/").casefold()
    expected_value = str(expected or "").strip().replace("\\", "/").casefold()
    if not actual_value or not expected_value:
        return False
    return actual_value == expected_value or Path(actual_value).name == Path(expected_value).name


def _metric_detail(
    *,
    retrieved_depth: int,
    reference_count: int,
    matched_evidence: dict[str, int],
    unavailable_reason: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    detail = {
        "definition_version": METRIC_DEFINITIONS_VERSION,
        "retrieved_depth": retrieved_depth,
        "reference_count": reference_count,
        "matched_evidence": matched_evidence,
        **extra,
    }
    if unavailable_reason:
        detail["unavailable_reason"] = unavailable_reason
    return detail


def _ranking_judgments(
    ranking_judgments: Any,
) -> tuple[dict[str, int] | None, dict[str, Any]]:
    """Validate and normalize optional qrels without exposing their raw contents."""
    if ranking_judgments is None:
        return None, {
            "definition_version": METRIC_DEFINITIONS_VERSION,
            "judged_universe_id": None,
            "corpus_fingerprint": None,
            "qrels_sha256": None,
            "judged_count": 0,
            "unavailable_reason": "missing_qrels",
        }

    if not isinstance(ranking_judgments, dict):
        raise ValueError("ranking_judgments must be an object")

    qrels = ranking_judgments.get("qrels")
    if qrels is None:
        return None, {
            "definition_version": METRIC_DEFINITIONS_VERSION,
            "judged_universe_id": ranking_judgments.get("universe_id"),
            "corpus_fingerprint": ranking_judgments.get("corpus_fingerprint"),
            "qrels_sha256": None,
            "judged_count": 0,
            "unavailable_reason": "missing_qrels",
        }

    if ranking_judgments.get("schema_version") != 1:
        raise ValueError("ranking_judgments.schema_version must be 1")

    universe_id = ranking_judgments.get("universe_id")
    corpus_fingerprint = ranking_judgments.get("corpus_fingerprint")
    if not isinstance(universe_id, str) or not universe_id.strip():
        raise ValueError("ranking_judgments.universe_id must be a non-empty string")
    if not isinstance(corpus_fingerprint, str) or not corpus_fingerprint.strip():
        raise ValueError(
            "ranking_judgments.corpus_fingerprint must be a non-empty string"
        )
    if not isinstance(qrels, list):
        raise ValueError("ranking_judgments.qrels must be a list")

    normalized_qrels: dict[str, int] = {}
    for index, item in enumerate(qrels, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"ranking_judgments.qrels[{index}] must be an object")
        candidate_id = item.get("candidate_id")
        relevance = item.get("relevance")
        if not isinstance(candidate_id, str) or not candidate_id.strip():
            raise ValueError(
                f"ranking_judgments.qrels[{index}].candidate_id must be a non-empty string"
            )
        if isinstance(relevance, bool) or not isinstance(relevance, int) or not 0 <= relevance <= 3:
            raise ValueError(
                f"ranking_judgments.qrels[{index}].relevance must be an integer from 0 to 3"
            )
        candidate_id = candidate_id.strip()
        if candidate_id in normalized_qrels:
            raise ValueError(f"Duplicate qrel candidate_id: {candidate_id}")
        normalized_qrels[candidate_id] = relevance

    canonical_qrels = [
        {"candidate_id": candidate_id, "relevance": normalized_qrels[candidate_id]}
        for candidate_id in sorted(normalized_qrels)
    ]
    return normalized_qrels, {
        "definition_version": METRIC_DEFINITIONS_VERSION,
        "judged_universe_id": universe_id.strip(),
        "corpus_fingerprint": corpus_fingerprint.strip(),
        "qrels_sha256": _canonical_sha256(canonical_qrels),
        "judged_count": len(normalized_qrels),
    }


def _ranking_candidate_ids(chunks: list[dict], *, validate: bool) -> list[str | None]:
    candidate_ids: list[str | None] = []
    seen: set[str] = set()
    for rank, chunk in enumerate(chunks or [], start=1):
        raw_candidate_id = chunk.get("candidate_id")
        if raw_candidate_id is None:
            candidate_ids.append(None)
            continue
        if not isinstance(raw_candidate_id, str) or not raw_candidate_id.strip():
            raise ValueError(f"Ranking candidate at rank {rank} has an invalid candidate_id")
        candidate_id = raw_candidate_id.strip()
        if validate and candidate_id in seen:
            raise ValueError(f"Duplicate ranking candidate_id: {candidate_id}")
        seen.add(candidate_id)
        candidate_ids.append(candidate_id)
    return candidate_ids


def _ndcg_at_10(
    chunks: list[dict],
    ranking_judgments: Any,
    *,
    reference_count: int,
    matched_evidence: dict[str, int],
) -> tuple[float | None, dict[str, Any]]:
    qrels, judgment_details = _ranking_judgments(ranking_judgments)
    retrieved_depth = len(chunks or [])
    if qrels is None:
        return None, _metric_detail(
            retrieved_depth=retrieved_depth,
            reference_count=reference_count,
            matched_evidence=matched_evidence,
            judged_universe_id=judgment_details.get("judged_universe_id"),
            corpus_fingerprint=judgment_details.get("corpus_fingerprint"),
            qrels_sha256=judgment_details.get("qrels_sha256"),
            judged_count=judgment_details.get("judged_count", 0),
            unavailable_reason=judgment_details["unavailable_reason"],
        )

    candidate_ids = _ranking_candidate_ids(chunks or [], validate=True)
    top_candidate_ids = candidate_ids[:10]
    missing_ids = [candidate_id for candidate_id in candidate_ids if candidate_id is None]
    if missing_ids:
        reason = "candidate_id_missing"
        return None, _metric_detail(
            retrieved_depth=retrieved_depth,
            reference_count=reference_count,
            matched_evidence=matched_evidence,
            judged_universe_id=judgment_details["judged_universe_id"],
            corpus_fingerprint=judgment_details["corpus_fingerprint"],
            qrels_sha256=judgment_details["qrels_sha256"],
            judged_count=judgment_details["judged_count"],
            unavailable_reason=reason,
        )

    unjudged_ids = [candidate_id for candidate_id in candidate_ids if candidate_id not in qrels]
    if unjudged_ids:
        return None, _metric_detail(
            retrieved_depth=retrieved_depth,
            reference_count=reference_count,
            matched_evidence=matched_evidence,
            judged_universe_id=judgment_details["judged_universe_id"],
            corpus_fingerprint=judgment_details["corpus_fingerprint"],
            qrels_sha256=judgment_details["qrels_sha256"],
            judged_count=judgment_details["judged_count"],
            unavailable_reason="candidate_not_judged",
            unjudged_candidate_count=len(unjudged_ids),
        )

    ideal_relevances = sorted(qrels.values(), reverse=True)[:10]
    idcg = sum(
        (2**relevance - 1) / math.log2(rank + 1)
        for rank, relevance in enumerate(ideal_relevances, start=1)
    )
    base_detail = {
        "judged_universe_id": judgment_details["judged_universe_id"],
        "corpus_fingerprint": judgment_details["corpus_fingerprint"],
        "qrels_sha256": judgment_details["qrels_sha256"],
        "judged_count": judgment_details["judged_count"],
        "idcg_at_10": round(idcg, 4),
    }
    if idcg == 0:
        return None, _metric_detail(
            retrieved_depth=retrieved_depth,
            reference_count=reference_count,
            matched_evidence=matched_evidence,
            unavailable_reason="idcg_zero",
            **base_detail,
        )

    dcg = sum(
        (2 ** qrels[candidate_id] - 1) / math.log2(rank + 1)
        for rank, candidate_id in enumerate(top_candidate_ids, start=1)
    )
    ndcg = round(dcg / idcg, 4)
    return ndcg, _metric_detail(
        retrieved_depth=retrieved_depth,
        reference_count=reference_count,
        matched_evidence=matched_evidence,
        **base_detail,
    )


def _retrieval_relevance(
    chunks: list[dict],
    reference_evidence: Any,
) -> tuple[bool | None, dict[str, Any]]:
    specs = _evidence_specs(reference_evidence)
    if not specs:
        return None, {"reason": "missing_reference_evidence", "matches": []}

    matches: list[dict[str, Any]] = []
    for evidence_index, spec in enumerate(specs, start=1):
        for chunk_index, chunk in enumerate(chunks or [], start=1):
            source_matches = not spec["source"] or _source_matches(
                chunk.get("filename"),
                spec["source"],
            )
            normalized_content = normalize_text(str(chunk.get("content") or ""))
            terms_match = all(
                normalize_text(term) in normalized_content
                for term in spec["contains"]
            )
            if source_matches and terms_match:
                matches.append(
                    {
                        "evidence_index": evidence_index,
                        "chunk_index": chunk_index,
                        "source": chunk.get("filename"),
                    }
                )

    return bool(matches), {"matches": matches}


def _retrieval_metrics(
    chunks: list[dict],
    reference_evidence: Any,
    ranking_judgments: Any = None,
) -> tuple[dict[str, float | None], dict[str, Any]]:
    specs = _evidence_specs(reference_evidence)
    retrieved_depth = len(chunks or [])
    if not specs:
        values = {
            "recall_at_10": None,
            "recall_at_20": None,
            "evidence_discounted_coverage_at_10": None,
            "ndcg_at_10": None,
        }
        empty_matches: dict[str, int] = {}
        unavailable = "missing_reference_evidence"
        ndcg, ndcg_details = _ndcg_at_10(
            chunks,
            ranking_judgments,
            reference_count=0,
            matched_evidence=empty_matches,
        )
        values["ndcg_at_10"] = ndcg
        return values, {
            "definition_version": METRIC_DEFINITIONS_VERSION,
            "reason": unavailable,
            "unavailable_reason": unavailable,
            "retrieved_depth": retrieved_depth,
            "reference_count": 0,
            "matched_evidence": empty_matches,
            "recall_at_10": _metric_detail(
                retrieved_depth=retrieved_depth,
                reference_count=0,
                matched_evidence=empty_matches,
                unavailable_reason=unavailable,
            ),
            "recall_at_20": _metric_detail(
                retrieved_depth=retrieved_depth,
                reference_count=0,
                matched_evidence=empty_matches,
                unavailable_reason=unavailable,
            ),
            "evidence_discounted_coverage_at_10": _metric_detail(
                retrieved_depth=retrieved_depth,
                reference_count=0,
                matched_evidence=empty_matches,
                unavailable_reason=unavailable,
            ),
            "ndcg_at_10": ndcg_details,
        }

    matched_evidence: dict[int, int] = {}
    for rank, chunk in enumerate(chunks or [], start=1):
        normalized_content = normalize_text(str(chunk.get("content") or ""))
        for evidence_index, spec in enumerate(specs, start=1):
            if evidence_index in matched_evidence:
                continue
            source_matches = not spec["source"] or _source_matches(
                chunk.get("filename"),
                spec["source"],
            )
            terms_match = all(
                normalize_text(term) in normalized_content
                for term in spec["contains"]
            )
            if source_matches and terms_match:
                matched_evidence[evidence_index] = rank

    def recall_at(k: int) -> float:
        found = sum(1 for rank in matched_evidence.values() if rank <= k)
        return round(found / len(specs), 4)

    discounted_sum = sum(
        1 / math.log2(rank + 1)
        for rank in matched_evidence.values()
        if rank <= 10
    )
    evidence_discounted_coverage = round(discounted_sum / len(specs), 4)
    base_details = {
        "definition_version": METRIC_DEFINITIONS_VERSION,
        "retrieved_depth": retrieved_depth,
        "reference_count": len(specs),
        "matched_evidence": {
            str(evidence_index): rank
            for evidence_index, rank in sorted(matched_evidence.items())
        },
    }
    ndcg, ndcg_details = _ndcg_at_10(
        chunks,
        ranking_judgments,
        reference_count=len(specs),
        matched_evidence=base_details["matched_evidence"],
    )
    return (
        {
            "recall_at_10": recall_at(10),
            "recall_at_20": recall_at(20),
            "evidence_discounted_coverage_at_10": evidence_discounted_coverage,
            "ndcg_at_10": ndcg,
        },
        {
            **base_details,
            "recall_at_10": _metric_detail(**base_details),
            "recall_at_20": _metric_detail(**base_details),
            "evidence_discounted_coverage_at_10": _metric_detail(
                **base_details,
                coverage_weight_sum=round(discounted_sum, 4),
            ),
            "ndcg_at_10": ndcg_details,
        },
    )


def _sanitize_evaluation_metadata(value: Any) -> Any:
    """Remove payloads de conteúdo de artefatos de comparação."""
    if isinstance(value, dict):
        return {
            str(key): _sanitize_evaluation_metadata(item)
            for key, item in value.items()
            if key not in {"content", "text", "answer", "question", "state"}
        }
    if isinstance(value, list):
        return [_sanitize_evaluation_metadata(item) for item in value]
    if isinstance(value, tuple):
        return [_sanitize_evaluation_metadata(item) for item in value]
    return value


def _stage_unavailable_metrics(
    *,
    stage: dict[str, Any],
    reference_count: int,
    reason: str,
) -> tuple[dict[str, None], dict[str, Any]]:
    retrieved_depth = int(stage.get("count") or len(stage.get("ids") or []))
    matched: dict[str, int] = {}
    values = {
        "recall_at_10": None,
        "recall_at_20": None,
        "evidence_discounted_coverage_at_10": None,
        "ndcg_at_10": None,
    }
    details = {
        metric_name: _metric_detail(
            retrieved_depth=retrieved_depth,
            reference_count=reference_count,
            matched_evidence=matched,
            unavailable_reason=reason,
        )
        for metric_name in values
    }
    return values, details


def _opaque_report_id(value: Any, *, prefix: str = "candidate") -> str:
    text = str(value or "").strip()
    if re.fullmatch(r"(?:candidate|section)-[0-9a-f]{24}", text):
        return text
    return f"{prefix}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:24]}"


def _sanitize_stage_for_report(stage: Any, *, stage_name: str = "") -> dict[str, Any]:
    if not isinstance(stage, dict):
        return {
            "status": "unavailable",
            "ids": [],
            "order": [],
            "count": 0,
            "reason": "stage_not_declared",
        }
    sanitized = _sanitize_evaluation_metadata(
        {key: value for key, value in stage.items() if key != "chunks"}
    )
    if not isinstance(sanitized, dict):
        return {}
    prefix = "section" if stage_name == "sections" else "candidate"
    for key in ("ids", "order"):
        values = sanitized.get(key)
        if isinstance(values, list):
            sanitized[key] = [_opaque_report_id(value, prefix=prefix) for value in values]
    exclusions = sanitized.get("excluded", sanitized.get("exclusions"))
    if isinstance(exclusions, list):
        safe_exclusions = []
        for item in exclusions:
            if not isinstance(item, dict):
                continue
            safe_item = {
                key: item[key]
                for key in ("rank", "reason")
                if key in item
            }
            if safe_item:
                safe_exclusions.append(safe_item)
        if "excluded" in sanitized:
            sanitized["excluded"] = safe_exclusions
        else:
            sanitized["exclusions"] = safe_exclusions
    evidence = sanitized.get("evidence")
    if isinstance(evidence, list):
        safe_evidence = []
        for item in evidence:
            if not isinstance(item, dict):
                continue
            safe_item = {
                key: item[key]
                for key in (
                    "source",
                    "document_id",
                    "section_id",
                    "content_hash",
                    "location",
                    "spans",
                    "rank",
                    "retrieval_origin",
                    "is_neighbor",
                    "seed_chunk_id",
                )
                if key in item
            }
            if "evidence_id" in item:
                safe_item["evidence_id"] = _opaque_report_id(
                    item["evidence_id"], prefix="candidate"
                )
            if "candidate_id" in item and item["candidate_id"]:
                safe_item["candidate_id"] = _opaque_report_id(
                    item["candidate_id"], prefix="candidate"
                )
            safe_evidence.append(safe_item)
        sanitized["evidence"] = safe_evidence
    return sanitized


def _sanitize_context_selection(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    sanitized = _sanitize_evaluation_metadata(value)
    if not isinstance(sanitized, dict):
        return {}
    retained = sanitized.get("retained_evidence")
    if isinstance(retained, list):
        sanitized["retained_evidence"] = [
            _sanitize_stage_for_report({"evidence": [item]}).get("evidence", [{}])[0]
            for item in retained
            if isinstance(item, dict)
        ]
    order = sanitized.get("order")
    if isinstance(order, list):
        sanitized["order"] = [
            _opaque_report_id(item, prefix="candidate") for item in order
        ]
    exclusions = sanitized.get("exclusions")
    if isinstance(exclusions, list):
        safe_exclusions = []
        for item in exclusions:
            if not isinstance(item, dict):
                continue
            safe_item = {
                key: item[key]
                for key in (
                    "index",
                    "rank",
                    "reason",
                    "limit",
                    "source",
                    "document_id",
                    "section_id",
                    "content_hash",
                    "location",
                    "spans",
                    "retrieval_origin",
                    "is_neighbor",
                    "seed_chunk_id",
                )
                if key in item
            }
            for key in ("evidence_id", "candidate_id", "id", "chunk_id"):
                if key in item and item[key] is not None:
                    safe_item[key] = _opaque_report_id(item[key], prefix="candidate")
            if safe_item:
                safe_exclusions.append(safe_item)
        sanitized["exclusions"] = safe_exclusions
    return sanitized


def _rendered_context_chunks(
    chunks: list[dict], trace: dict[str, Any]
) -> list[dict]:
    """Recorta os chunks finais aos spans realmente renderizados no prompt."""
    selection = trace.get("context_selection")
    retained = selection.get("retained_evidence") if isinstance(selection, dict) else None
    if not isinstance(retained, list):
        return list(chunks or [])

    evidence_by_key: dict[str, dict[str, Any]] = {}
    for evidence in retained:
        if not isinstance(evidence, dict):
            continue
        for key in (
            evidence.get("evidence_id"),
            evidence.get("candidate_id"),
            evidence.get("content_hash"),
        ):
            if key is not None and str(key).strip():
                evidence_by_key[str(key)] = evidence

    rendered: list[dict] = []
    for chunk in chunks or []:
        if not isinstance(chunk, dict):
            continue
        content = str(chunk.get("content") or "")
        content_hash = str(chunk.get("content_hash") or "").strip()
        if not content_hash:
            metadata = chunk.get("metadata")
            if isinstance(metadata, dict):
                content_hash = str(metadata.get("content_hash") or "").strip()
        if not content_hash:
            content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        keys = (
            chunk.get("evidence_id"),
            chunk.get("candidate_id"),
            chunk.get("id"),
            content_hash,
        )
        evidence = next(
            (
                evidence_by_key.get(str(key))
                for key in keys
                if key is not None and str(key).strip() and str(key) in evidence_by_key
            ),
            None,
        )
        if evidence is None:
            continue
        spans = evidence.get("spans")
        if not isinstance(spans, list):
            continue
        rendered_parts = []
        for span in spans:
            if not isinstance(span, dict):
                continue
            start = max(0, int(span.get("start") or 0))
            end = min(len(content), int(span.get("end") or start))
            if end > start:
                rendered_parts.append(content[start:end])
        if rendered_parts:
            rendered_chunk = dict(chunk)
            rendered_chunk["content"] = "\n".join(rendered_parts)
            rendered.append(rendered_chunk)
    return rendered


def _evaluate_retrieval_stages(
    *,
    case: dict[str, Any],
    final_chunks: list[dict],
    trace: dict[str, Any],
) -> dict[str, Any]:
    declared = trace.get("retrieval_stages")
    if not isinstance(declared, dict):
        return {}

    stage_results: dict[str, Any] = {}
    reference_count = len(_evidence_specs(case.get("reference_evidence")))
    for stage_name in RETRIEVAL_STAGE_NAMES:
        raw_stage = declared.get(stage_name)
        stage = raw_stage if isinstance(raw_stage, dict) else {}
        if stage_name == "final_context":
            stage_chunks = _rendered_context_chunks(final_chunks, trace)
        else:
            stage_chunks = stage.get("chunks")
            if (
                not isinstance(stage_chunks, list)
                and stage.get("count") == len(final_chunks)
                and final_chunks
            ):
                stage_chunks = final_chunks
        status = str(stage.get("status") or "available")
        if status not in {"available", "complete"}:
            metrics, details = _stage_unavailable_metrics(
                stage=stage,
                reference_count=reference_count,
                reason=str(stage.get("reason") or "stage_not_available"),
            )
        elif not isinstance(stage_chunks, list):
            metrics, details = _stage_unavailable_metrics(
                stage=stage,
                reference_count=reference_count,
                reason=(
                    "stage_content_not_available"
                    if stage.get("count")
                    else "stage_empty"
                ),
            )
        else:
            metrics, details = _retrieval_metrics(
                stage_chunks,
                case.get("reference_evidence"),
                case.get("ranking_judgments"),
            )
        stage_results[stage_name] = {
            "stage": _sanitize_stage_for_report(stage, stage_name=stage_name),
            "metrics": metrics,
            "metric_details": details,
            "metric_observations": {
                metric_name: {
                    "value": value,
                    "denominator": int(
                        (details.get(metric_name) or {}).get("reference_count", 0)
                    )
                    if value is not None
                    else 0,
                    "unavailable_reason": (
                        (details.get(metric_name) or {}).get("unavailable_reason")
                        if isinstance(details.get(metric_name), dict)
                        else None
                    )
                    or ("metric_not_evaluated" if value is None else None),
                    "metric_version": str(METRIC_DEFINITIONS_VERSION),
                }
                for metric_name, value in metrics.items()
            },
        }
    return stage_results


def _citation_validity(
    abstained: bool,
    trace: dict[str, Any],
    reference_evidence: Any,
) -> tuple[bool | None, dict[str, Any]]:
    if abstained:
        return None, {"reason": "abstention_not_applicable", "matched_sources": []}

    expected_sources = [
        spec["source"]
        for spec in _evidence_specs(reference_evidence)
        if spec["source"]
    ]
    if not expected_sources:
        return None, {"reason": "missing_reference_sources", "matched_sources": []}

    grounding_errors = list(trace.get("grounding_errors") or [])
    cited_sources = list(trace.get("cited_files") or trace.get("citations") or [])
    matched_sources = [
        cited
        for cited in cited_sources
        if any(_source_matches(cited, expected) for expected in expected_sources)
    ]
    valid = not grounding_errors and bool(matched_sources)
    return valid, {
        "grounding_errors": grounding_errors,
        "cited_sources": cited_sources,
        "matched_sources": matched_sources,
    }


def _grounded(abstained: bool, trace: dict[str, Any]) -> bool:
    grounding_errors = trace.get("grounding_errors") or []
    if abstained:
        return len(grounding_errors) == 0 or trace.get("abstention_reason") in {
            "no_chunks",
            "few_chunks",
            "low_similarity",
            "low_similarity_operational",
            "no_context_after_merge",
            "grounding_validation_failed",
        }
    return len(grounding_errors) == 0


def _score_case(
    *,
    expected_behavior: str,
    abstained: bool,
    behavior_match: bool,
    intent_match: bool,
    factual_correctness: bool | None,
    retrieval_relevance: bool | None,
    citation_validity: bool | None,
) -> float | None:
    expected = (expected_behavior or "exact_answer").strip().lower()
    if expected in {"exact_answer", "partial_abstain"} and not abstained:
        if factual_correctness is None:
            return None

    weighted_metrics: list[tuple[bool, float]] = [
        (behavior_match, 0.25),
        (intent_match, 0.10),
    ]
    for value, weight in (
        (factual_correctness, 0.35),
        (retrieval_relevance, 0.15),
        (citation_validity, 0.15),
    ):
        if value is not None:
            weighted_metrics.append((value, weight))

    denominator = sum(weight for _value, weight in weighted_metrics)
    if denominator <= 0:
        return None
    score = sum(weight for value, weight in weighted_metrics if value) / denominator
    return round(score, 4)


def _evaluate_response(
    *,
    case: dict[str, Any],
    answer: str,
    chunks: list[dict],
    trace: dict[str, Any],
) -> dict[str, Any]:
    expected_behavior = str(case.get("expected_behavior") or "exact_answer").strip().lower()
    expected_intent = str(case.get("expected_intent") or "general").strip().lower()
    predicted_intent = str((trace.get("query_plan") or {}).get("intent") or "general").strip().lower()
    abstained = _is_abstained(answer, trace)
    clarified = _is_clarification(answer, trace)
    behavior_match = _behavior_match(
        expected_behavior,
        abstained,
        clarified,
        answer,
    )
    factual_correctness, factual_details = _factual_correctness(
        answer,
        abstained,
        case.get("expected_facts"),
    )
    retrieval_relevance, retrieval_details = _retrieval_relevance(
        chunks,
        case.get("reference_evidence"),
    )
    retrieval_metrics, ranked_retrieval_details = _retrieval_metrics(
        chunks,
        case.get("reference_evidence"),
        case.get("ranking_judgments"),
    )
    stage_metrics = _evaluate_retrieval_stages(
        case=case,
        final_chunks=chunks,
        trace=trace,
    )
    citation_validity, citation_details = _citation_validity(
        abstained,
        trace,
        case.get("reference_evidence"),
    )
    intent_match = expected_intent == predicted_intent
    grounded = _grounded(abstained, trace)
    false_abstention = _false_abstention(expected_behavior, abstained)
    false_absence_claim = _false_absence_claim(
        expected_behavior,
        abstained,
        answer,
    )
    forbidden_matches = _forbidden_fact_matches(answer, case.get("forbidden_facts"))
    unsupported_claims = None
    if not abstained and not clarified:
        unsupported_claims = bool(forbidden_matches or trace.get("grounding_errors"))
    score = _score_case(
        expected_behavior=expected_behavior,
        abstained=abstained,
        behavior_match=behavior_match,
        intent_match=intent_match,
        factual_correctness=factual_correctness,
        retrieval_relevance=retrieval_relevance,
        citation_validity=citation_validity,
    )
    return {
        "expected_behavior": expected_behavior,
        "expected_intent": expected_intent,
        "predicted_intent": predicted_intent,
        "abstained": abstained,
        "clarified": clarified,
        "behavior_match": behavior_match,
        "factual_correctness": factual_correctness,
        "retrieval_relevance": retrieval_relevance,
        **retrieval_metrics,
        "citation_validity": citation_validity,
        "citation_ok": citation_validity,
        "intent_match": intent_match,
        "grounded": grounded,
        "false_abstention": false_abstention,
        "false_absence_claim": false_absence_claim,
        "unsupported_claims": unsupported_claims,
        "score": score,
        "stage_metrics": stage_metrics,
        "metric_details": {
            "factual_correctness": factual_details,
            "retrieval_relevance": retrieval_details,
            "recall_at_10": ranked_retrieval_details["recall_at_10"],
            "recall_at_20": ranked_retrieval_details["recall_at_20"],
            "evidence_discounted_coverage_at_10": ranked_retrieval_details[
                "evidence_discounted_coverage_at_10"
            ],
            "ndcg_at_10": ranked_retrieval_details["ndcg_at_10"],
            "citation_validity": citation_details,
            "false_abstention": {"value": false_abstention},
            "false_absence_claim": {"value": false_absence_claim},
            "unsupported_claims": {
                "value": unsupported_claims,
                "forbidden_fact_matches": forbidden_matches,
                "grounding_errors": list(trace.get("grounding_errors") or []),
            },
            "stages": {
                stage_name: {
                    "metric_details": value["metric_details"],
                    "stage": value["stage"],
                }
                for stage_name, value in stage_metrics.items()
            },
        },
    }


def _git_commit() -> str | None:
    configured_commit = str(os.getenv("BENCHMARK_GIT_COMMIT") or "").strip()
    if configured_commit:
        return configured_commit
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT_DIR,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    commit = completed.stdout.strip()
    return commit if completed.returncode == 0 and commit else None


def _canonical_sha256(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _text_identity(text: str) -> dict[str, Any]:
    encoded = (text or "").encode("utf-8")
    return {
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "chars": len(text or ""),
    }


def _database_identity() -> dict[str, Any]:
    if not os.getenv("DATABASE_URL"):
        return {
            "status": "unknown",
            "reason": "database_not_configured",
            "schema_version": None,
            "corpus": None,
            "feedback": None,
            "persisted_vector_identities": None,
        }

    try:
        rows = rag.supabase_rpc("get_evaluation_data_identity", {})
    except Exception as exc:
        return {
            "status": "unknown",
            "reason": "identity_query_unavailable",
            "error_type": type(exc).__name__,
            "schema_version": None,
            "corpus": None,
            "feedback": None,
            "persisted_vector_identities": None,
        }

    if not rows or not isinstance(rows[0], dict):
        return {
            "status": "unknown",
            "reason": "identity_query_returned_no_rows",
            "schema_version": None,
            "corpus": None,
            "feedback": None,
            "persisted_vector_identities": None,
        }

    row = rows[0]
    persisted_identities = row.get("vector_index_identities")
    if not isinstance(persisted_identities, list):
        persisted_identities = []
    persisted_identities = sorted(
        (identity for identity in persisted_identities if isinstance(identity, dict)),
        key=lambda identity: str(identity.get("index_scope") or ""),
    )
    return {
        "status": "verified",
        "reason": None,
        "schema_version": row.get("schema_version"),
        "corpus": {
            "sha256": row.get("corpus_sha256"),
            "document_count": row.get("corpus_document_count"),
            "section_count": row.get("corpus_section_count"),
            "chunk_count": row.get("corpus_chunk_count"),
        },
        "feedback": {
            "sha256": row.get("feedback_sha256"),
            "item_count": row.get("feedback_item_count"),
            "chunk_count": row.get("feedback_chunk_count"),
        },
        "persisted_vector_identities": persisted_identities,
    }


def _vector_identity_metadata(database_identity: dict[str, Any]) -> dict[str, Any]:
    configured = rag.get_embedding_index_identity()
    persisted_rows = database_identity.get("persisted_vector_identities")
    persisted_by_scope = {
        str(row.get("index_scope")): {
            field: row.get(field)
            for field in ("provider", "model", "dimensions", "preprocessing_version")
        }
        for row in (persisted_rows or [])
        if isinstance(row, dict) and row.get("index_scope")
    }

    scopes: dict[str, Any] = {}
    for scope in ("corpus", "sections", "feedback"):
        persisted = persisted_by_scope.get(scope)
        if persisted is None:
            status = "unknown"
        elif persisted == configured:
            status = "verified"
        else:
            status = "mismatch"
        scopes[scope] = {
            "status": status,
            "configured": configured,
            "persisted": persisted,
        }
    return scopes


def _prompt_and_policy_identity(
    baseline_config: dict[str, Any] | None,
) -> dict[str, Any]:
    business_rules = rag._load_business_rules_context()
    full_context = rag._load_full_context_docs() if config.FULL_CONTEXT_ENABLED else ""
    return {
        "system_prompt": _text_identity(config.SYSTEM_PROMPT),
        "no_answer_policy": _text_identity(config.NO_ANSWER_PHRASE),
        "clarification_policy": _text_identity(config.ABSTAIN_CLARIFYING_QUESTION),
        "routing_policy": {
            "sha256": _canonical_sha256(
                {
                    "intent_priority": rag.INTENT_PRIORITY,
                    "intent_keywords": rag.INTENT_KEYWORDS,
                    "module_hints": rag.QUERY_MODULE_HINTS,
                    "default_modules": rag.INTENT_DEFAULT_MODULES,
                    "doc_types": rag.INTENT_DOC_TYPES,
                }
            )
        },
        "response_policy": {
            "sha256": _canonical_sha256(rag.INTENT_RESPONSE_INSTRUCTIONS),
        },
        "query_preprocessing_policy": {
            "sha256": _canonical_sha256(rag.QUERY_ABBREVIATIONS),
        },
        "business_rules": {
            "enabled": bool(config.RAG_ENABLE_BUSINESS_RULES),
            **_text_identity(business_rules),
        },
        "full_context": {
            "enabled": bool(config.FULL_CONTEXT_ENABLED),
            **_text_identity(full_context),
        },
        "baseline_policy_sha256": (
            _canonical_sha256(baseline_config)
            if baseline_config is not None
            else None
        ),
    }


def _jev_identity() -> dict[str, Any]:
    """Identidade segura do contrato Jev, sem chave ou conteudo de perguntas."""
    prompt_contract = {
        "question_types": ["choice", "noul"],
        "noul_fields": ["type", "noul"],
        "choice_fields": ["type", "choice", "probabilities", "confidence"],
        "answer_ids": "must_match_question_ids",
    }
    return {
        "configured": bool(config.TYPESAFE_API_KEY),
        "endpoint": jev.ENDPOINT,
        "model": config.JEV_MODEL,
        "contract_version": jev.CONTRACT_VERSION,
        "pricing_version": jev.PRICING_VERSION,
        "input_price_usd_per_million": jev.INPUT_PRICE_USD_PER_MILLION,
        "prompt_contract": {
            "sha256": _canonical_sha256(prompt_contract),
            "version": jev.CONTRACT_VERSION,
        },
    }


def _experiment_identity(
    baseline_config: dict[str, Any] | None,
    *,
    comparison: dict[str, Any] | None = None,
) -> dict[str, Any]:
    database_identity = _database_identity()
    identity = {
        "schema_version": 1,
        "model_config": rag.get_model_config(),
        "rag_config": {
            field: getattr(config, field)
            for field in _CONFIG_FIELDS
            if hasattr(config, field)
        },
        "prompts_and_policies": _prompt_and_policy_identity(baseline_config),
        "database": database_identity,
        "vector_indexes": _vector_identity_metadata(database_identity),
        "jev": _jev_identity(),
        "cache": {
            "query_embedding": {
                "scope": "process_local",
                "max_entries": rag._query_embedding_cache._maxsize,
                "ttl_seconds": rag._query_embedding_cache._ttl,
                "identity_aware": True,
            },
            "business_rules": "process_local_by_path_and_mtime",
            "full_context": "process_local_by_max_mtime",
        },
        "evaluation_contract": {
            "evaluator_schema_version": EVALUATOR_SCHEMA_VERSION,
            "metric_definitions_version": METRIC_DEFINITIONS_VERSION,
            "metric_definitions_sha256": _canonical_sha256(METRIC_DEFINITIONS),
            "context_selection_version": getattr(
                rag, "CONTEXT_SELECTION_VERSION", None
            ),
            "token_counter_version": getattr(rag, "TOKEN_COUNTER_VERSION", None),
            "retrieval_stage_names": list(RETRIEVAL_STAGE_NAMES),
            "comparison_views": list(COMPARISON_VIEWS),
        },
    }
    if comparison is not None:
        identity["comparison"] = {
            key: comparison.get(key)
            for key in (
                "profile_version",
                "variant_id",
                "pair_id",
                "snapshot_id",
                "view",
            )
        }
    return {**identity, "fingerprint_sha256": _canonical_sha256(identity)}


def _runtime_metadata(
    dataset: list[dict[str, Any]],
    baseline_config: dict[str, Any] | None,
    *,
    comparison: dict[str, Any] | None = None,
) -> dict[str, Any]:
    canonical_dataset = json.dumps(
        dataset,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    experiment_identity = _experiment_identity(
        baseline_config,
        comparison=comparison,
    )
    return {
        "evaluator_schema_version": EVALUATOR_SCHEMA_VERSION,
        "metric_definitions_version": METRIC_DEFINITIONS_VERSION,
        "metric_definitions_sha256": _canonical_sha256(METRIC_DEFINITIONS),
        "git_commit": _git_commit(),
        "dataset_sha256": hashlib.sha256(canonical_dataset).hexdigest(),
        "model_config": rag.get_model_config(),
        "embedding_index_identity": rag.get_embedding_index_identity(),
        "jev": _jev_identity(),
        "comparison": comparison,
        "rag_config": {
            field: getattr(config, field)
            for field in _CONFIG_FIELDS
            if hasattr(config, field)
        },
        "baseline_config": baseline_config,
        "experiment_identity": experiment_identity,
    }


def _flatten_identity(value: Any, prefix: str = "") -> dict[str, Any]:
    if isinstance(value, dict):
        flattened: dict[str, Any] = {}
        for key in sorted(value):
            if key == "fingerprint_sha256":
                continue
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            flattened.update(_flatten_identity(value[key], child_prefix))
        return flattened
    if isinstance(value, list):
        return {prefix: value}
    return {prefix: value}


def _declared_difference(path: str, declared_variables: set[str]) -> bool:
    return any(
        path == variable or path.startswith(f"{variable}.")
        for variable in declared_variables
    )


def _has_identity_status(value: Any, statuses: set[str]) -> bool:
    if isinstance(value, dict):
        if value.get("status") in statuses:
            return True
        return any(_has_identity_status(item, statuses) for item in value.values())
    if isinstance(value, list):
        return any(_has_identity_status(item, statuses) for item in value)
    return False


def _compare_runtime_identities(
    current_runtime: dict[str, Any],
    reference_runtime: dict[str, Any],
    *,
    experimental_variables: list[str] | None = None,
) -> dict[str, Any]:
    current = current_runtime.get("experiment_identity")
    reference = reference_runtime.get("experiment_identity")
    if not isinstance(current, dict) or not isinstance(reference, dict):
        return {
            "status": "incomplete",
            "compatible": False,
            "declared_experimental_variables": sorted(experimental_variables or []),
            "differences": [],
            "reason": "experiment_identity_missing",
        }

    current_values = {
        **_flatten_identity(current),
        "evaluator_schema_version": current_runtime.get("evaluator_schema_version"),
        "metric_definitions_version": current_runtime.get("metric_definitions_version"),
        "metric_definitions_sha256": current_runtime.get("metric_definitions_sha256"),
        "git_commit": current_runtime.get("git_commit"),
        "dataset_sha256": current_runtime.get("dataset_sha256"),
        **_flatten_identity(current_runtime.get("selection"), "selection"),
    }
    reference_values = {
        **_flatten_identity(reference),
        "evaluator_schema_version": reference_runtime.get("evaluator_schema_version"),
        "metric_definitions_version": reference_runtime.get("metric_definitions_version"),
        "metric_definitions_sha256": reference_runtime.get("metric_definitions_sha256"),
        "git_commit": reference_runtime.get("git_commit"),
        "dataset_sha256": reference_runtime.get("dataset_sha256"),
        **_flatten_identity(reference_runtime.get("selection"), "selection"),
    }
    declared = {
        str(value).strip()
        for value in (experimental_variables or [])
        if str(value).strip()
    }
    differences = []
    for path in sorted(set(current_values) | set(reference_values)):
        current_value = current_values.get(path)
        reference_value = reference_values.get(path)
        if current_value == reference_value:
            continue
        differences.append(
            {
                "path": path,
                "reference": reference_value,
                "current": current_value,
                "declared_experimental_variable": _declared_difference(path, declared),
            }
        )

    undeclared = [
        difference
        for difference in differences
        if not difference["declared_experimental_variable"]
    ]
    unknown = _has_identity_status(current, {"unknown"}) or _has_identity_status(
        reference,
        {"unknown"},
    )
    mismatch = _has_identity_status(current, {"mismatch"}) or _has_identity_status(
        reference,
        {"mismatch"},
    )
    if undeclared or mismatch:
        status = "incompatible"
    elif unknown:
        status = "incomplete"
    elif differences:
        status = "compatible_with_declared_changes"
    else:
        status = "compatible"
    return {
        "status": status,
        "compatible": status in {"compatible", "compatible_with_declared_changes"},
        "declared_experimental_variables": sorted(declared),
        "differences": differences,
        "reason": (
            "vector_identity_mismatch"
            if mismatch
            else "unknown_identity"
            if status == "incomplete" and unknown
            else None
        ),
    }


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    position = max(0, math.ceil(len(ordered) * percentile) - 1)
    return ordered[position]


def _wilson_interval(successes: int, total: int, z: float = 1.96) -> dict[str, float] | None:
    if total <= 0:
        return None
    proportion = successes / total
    denominator = 1 + (z * z / total)
    center = (proportion + (z * z / (2 * total))) / denominator
    margin = (
        z
        * math.sqrt(
            (proportion * (1 - proportion) / total)
            + (z * z / (4 * total * total))
        )
        / denominator
    )
    return {
        "level": 0.95,
        "low": round(max(0.0, center - margin), 4),
        "high": round(min(1.0, center + margin), 4),
        "method": "wilson",
    }


def _rate_summary(successes: int, total: int) -> dict[str, Any]:
    return {
        "count": successes,
        "denominator": total,
        "rate": round(successes / total, 4) if total else None,
        "confidence_interval": _wilson_interval(successes, total),
    }


def _case_outcome(result: dict[str, Any]) -> str:
    answerability = result["answerability"]
    if result["abstained"]:
        return "correct_abstention" if answerability == "no_evidence" else "incorrect_abstention"
    if result["clarified"]:
        return (
            "necessary_clarification"
            if answerability == "ambiguous"
            else "unnecessary_clarification"
        )
    if result.get("unsupported_claims"):
        return "unsupported_answer"
    if answerability == "answerable" and result.get("factual_correctness") is True:
        return "correct_answer"
    if answerability == "ambiguous":
        return "missing_clarification"
    if answerability == "no_evidence":
        return "unsupported_answer"
    return "incorrect_answer"


def _summarize_outcomes(results: list[dict[str, Any]]) -> dict[str, Any]:
    answerable = [result for result in results if result["answerability"] == "answerable"]
    ambiguous = [result for result in results if result["answerability"] == "ambiguous"]
    no_evidence = [result for result in results if result["answerability"] == "no_evidence"]
    answered = [
        result
        for result in results
        if not result["abstained"] and not result["clarified"]
    ]
    non_ambiguous = [
        result for result in results if result["answerability"] != "ambiguous"
    ]

    def count(outcome: str, population: list[dict[str, Any]]) -> int:
        return sum(1 for result in population if result["outcome"] == outcome)

    return {
        "correct_answers": _rate_summary(count("correct_answer", answerable), len(answerable)),
        "unsupported_answers": _rate_summary(
            count("unsupported_answer", answered),
            len(answered),
        ),
        "correct_abstentions": _rate_summary(
            count("correct_abstention", no_evidence),
            len(no_evidence),
        ),
        "incorrect_abstentions": _rate_summary(
            count("incorrect_abstention", answerable + ambiguous),
            len(answerable) + len(ambiguous),
        ),
        "necessary_clarifications": _rate_summary(
            count("necessary_clarification", ambiguous),
            len(ambiguous),
        ),
        "unnecessary_clarifications": _rate_summary(
            count("unnecessary_clarification", non_ambiguous),
            len(non_ambiguous),
        ),
        "answerable_coverage": _rate_summary(
            sum(
                1
                for result in answerable
                if not result["abstained"] and not result["clarified"]
            ),
            len(answerable),
        ),
        "population": {
            "answerable": len(answerable),
            "ambiguous": len(ambiguous),
            "no_evidence": len(no_evidence),
        },
    }


def _summarize_model_usage(results: list[dict[str, Any]]) -> dict[str, Any]:
    model_calls = [
        call
        for result in results
        for call in (result.get("trace") or {}).get("model_calls", [])
        if isinstance(call, dict)
    ]
    external_calls = [
        call
        for result in results
        for call in (result.get("trace") or {}).get("external_calls", [])
        if isinstance(call, dict)
    ]
    calls = model_calls + external_calls
    token_fields = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "total_tokens",
    )
    totals: dict[str, int | None] = {}
    for field in token_fields:
        values = [
            call.get("usage", {}).get(field)
            for call in model_calls
            if isinstance(call.get("usage"), dict)
        ]
        totals[field] = (
            sum(int(value) for value in values)
            if len(values) == len(model_calls)
            and all(value is not None for value in values)
            else None
        )

    known_costs = [
        float(call["estimated_cost_usd"])
        for call in calls
        if call.get("estimated_cost_usd") is not None
    ]
    cost_complete = bool(calls) and len(known_costs) == len(calls)
    return {
        "call_count": len(calls),
        "model_call_count": len(model_calls),
        "external_call_count": len(external_calls),
        "calls_with_usage": sum(1 for call in calls if call.get("usage") is not None),
        "calls_without_usage": sum(1 for call in calls if call.get("usage") is None),
        "by_stage": {
            stage: sum(1 for call in calls if call.get("stage") == stage)
            for stage in sorted(
                {str(call.get("stage") or "unknown") for call in calls}
            )
        },
        "totals": totals,
        "known_estimated_cost_usd": round(sum(known_costs), 12) if known_costs else None,
        "estimated_cost_usd": round(sum(known_costs), 12) if cost_complete else None,
        "cost_complete": cost_complete,
    }


def _nested_value(payload: dict[str, Any], path: str) -> Any:
    value: Any = payload
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _evaluate_non_regression(
    holdout_summary: dict[str, Any] | None,
    baseline_config: dict[str, Any] | None,
    *,
    expected_holdout_cases: int | None = None,
) -> dict[str, Any] | None:
    rules = (baseline_config or {}).get("non_regression")
    if not isinstance(rules, list):
        return None

    checks: list[dict[str, Any]] = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        actual = _nested_value(holdout_summary or {}, str(rule.get("path") or ""))
        operator = str(rule.get("operator") or "")
        expected = rule.get("value")
        passed = None
        if actual is not None and isinstance(expected, (int, float)):
            if operator == ">=":
                passed = float(actual) >= float(expected)
            elif operator == "<=":
                passed = float(actual) <= float(expected)
        checks.append(
            {
                "id": rule.get("id"),
                "description": rule.get("description"),
                "critical": bool(rule.get("critical")),
                "path": rule.get("path"),
                "operator": operator,
                "expected": expected,
                "actual": actual,
                "passed": passed,
            }
        )

    evaluated = [check for check in checks if check["passed"] is not None]
    failed = [check for check in evaluated if not check["passed"]]
    missing_checks = [check["id"] for check in checks if check["passed"] is None]
    holdout = holdout_summary or {}
    actual_cases = holdout.get("sample_size", 0)
    population = (holdout.get("outcomes") or {}).get("population", {})
    missing_populations = [
        name
        for name in ("answerable", "ambiguous", "no_evidence")
        if not population.get(name)
    ]
    coverage_complete = (
        expected_holdout_cases is not None
        and expected_holdout_cases > 0
        and actual_cases == expected_holdout_cases
        and not missing_populations
    )
    complete = (
        coverage_complete
        and bool(checks)
        and not missing_checks
        and len(checks) == len(rules)
    )
    return {
        "status": "failed" if failed else "passed" if complete else "incomplete",
        "complete": complete,
        "missing_checks": missing_checks,
        "coverage": {
            "expected_holdout_cases": expected_holdout_cases,
            "evaluated_holdout_cases": actual_cases,
            "missing_populations": missing_populations,
            "complete": coverage_complete,
        },
        "checks": checks,
        "critical_failures": [
            check["id"] for check in failed if check["critical"]
        ],
    }


def _trace_for_report(
    trace: dict[str, Any],
    comparison: dict[str, Any],
) -> dict[str, Any]:
    safe_trace = _sanitize_evaluation_metadata({**trace, "comparison": comparison})
    if not isinstance(safe_trace, dict):
        return {"comparison": comparison}
    stages = safe_trace.get("retrieval_stages")
    if isinstance(stages, dict):
        safe_trace["retrieval_stages"] = {
            stage_name: _sanitize_stage_for_report(stage, stage_name=stage_name)
            for stage_name, stage in stages.items()
        }
    for key in ("context_selection", "context_envelope"):
        if key in safe_trace:
            safe_trace[key] = _sanitize_context_selection(safe_trace[key])
    return safe_trace


def _insert_run(*, dataset_name: str, total_cases: int, metadata: dict[str, Any]) -> str:
    model_info = rag.get_model_config()
    row = rag.supabase_insert(
        "evaluation_runs",
        {
            "run_type": "offline",
            "dataset_name": dataset_name,
            "model": model_info.get("generation_model"),
            "embedding_model": model_info.get("embedding_model"),
            "total_cases": total_cases,
            "created_by": "offline_eval_script",
            "metadata": {
                **metadata,
                "llm_provider": model_info.get("llm_provider"),
                "embedding_provider": model_info.get("embedding_provider"),
            },
        },
    )
    if not row:
        raise RuntimeError("Failed to create evaluation run")
    return str(row[0]["id"])


def run_evaluation(
    *,
    dataset: list[dict[str, Any]],
    dataset_name: str,
    dry_run: bool,
    limit: int | None,
    answer_provider: AnswerProvider | None = None,
    baseline_config: dict[str, Any] | None = None,
    split: str = "all",
    reference_runtime: dict[str, Any] | None = None,
    experimental_variables: list[str] | None = None,
    variant_id: str = VARIANT_EXISTING,
    pair_id: str | None = None,
    snapshot_id: str | None = None,
    comparison_view: str = "end_to_end_same_snapshot",
    comparison_profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if comparison_view not in COMPARISON_VIEWS:
        raise VariantConfigurationError(
            f"Visão de comparação desconhecida: {comparison_view}."
        )
    profile = comparison_profile
    if (
        profile is None
        and variant_id != VARIANT_EXISTING
        and isinstance(baseline_config, dict)
    ):
        profile = _comparison_profile(baseline_config)
    variant_definition = _validate_variant_execution(
        variant_id,
        answer_provider=answer_provider,
        profile=profile if isinstance(profile, dict) else None,
    )
    comparison = _comparison_metadata(
        variant_id=variant_id,
        pair_id=pair_id,
        snapshot_id=snapshot_id,
        view=comparison_view,
        profile=profile if isinstance(profile, dict) else None,
    )
    comparison["available_without_provider"] = bool(
        variant_definition["available_without_provider"]
    )
    selected = [
        case
        for case in dataset
        if split == "all" or str(case.get("split") or "development") == split
    ]
    selected = selected[:limit] if limit and limit > 0 else selected
    if not selected:
        raise ValueError(f"Dataset has no cases for split: {split}")

    started_at = datetime.now(timezone.utc).isoformat()
    runtime_metadata = _runtime_metadata(
        dataset,
        baseline_config,
        comparison=comparison,
    )
    runtime_metadata["selection"] = {
        "split": split,
        "limit": limit,
        "case_ids": [
            str(case.get("id") or f"case-{index:04d}")
            for index, case in enumerate(selected, start=1)
        ],
    }
    runtime_comparison = (
        _compare_runtime_identities(
            runtime_metadata,
            reference_runtime,
            experimental_variables=experimental_variables,
        )
        if reference_runtime is not None
        else None
    )
    run_id = "DRY_RUN"
    if not dry_run:
        run_id = _insert_run(
            dataset_name=dataset_name,
            total_cases=len(selected),
            metadata={
                "started_at": started_at,
                "evaluator_schema_version": EVALUATOR_SCHEMA_VERSION,
                "runtime_comparison": runtime_comparison,
                **runtime_metadata,
            },
        )

    rows_to_store: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    paired_execution = pair_id is not None or snapshot_id is not None

    for index, case in enumerate(selected, start=1):
        case_id = str(case.get("id") or f"case-{index:04d}")
        question = str(case.get("question") or "").strip()
        scope = case.get("scope") if isinstance(case.get("scope"), dict) else {"level": "global"}
        conversation_history = (
            case.get("conversation_history")
            if isinstance(case.get("conversation_history"), list)
            else None
        )
        provider_scope = dict(scope)
        if paired_execution:
            provider_scope["_comparison_snapshot_id"] = snapshot_id
            provider_scope["_comparison_experiment_identity"] = runtime_metadata[
                "experiment_identity"
            ]

        t0 = time.perf_counter()
        if answer_provider is None:
            answer, chunks, trace = rag.ask(
                question,
                conversation_history=conversation_history,
                images=None,
                system_prompt=None,
                platform="offline_eval",
                scope=provider_scope,
            )
        else:
            answer, chunks, trace = answer_provider(question, provider_scope)
        if not isinstance(trace, dict):
            raise ValueError("O provider de avaliação deve retornar um trace objeto.")
        observed_snapshot = trace.get("snapshot_id")
        if paired_execution:
            if observed_snapshot is None:
                raise VariantConfigurationError(
                    "provider pareado não informou snapshot_id verificado."
                )
            if str(observed_snapshot) != str(snapshot_id):
                raise VariantConfigurationError(
                    "snapshot_id do provider diverge do snapshot pareado."
                )
        observed_identity = trace.get("experiment_identity")
        expected_identity = runtime_metadata.get("experiment_identity")
        if paired_execution and (
            not isinstance(observed_identity, dict)
            or not observed_identity.get("fingerprint_sha256")
        ):
            raise VariantConfigurationError(
                "provider pareado não informou experiment_identity verificada."
            )
        if (
            isinstance(observed_identity, dict)
            and isinstance(expected_identity, dict)
            and observed_identity.get("fingerprint_sha256")
            and expected_identity.get("fingerprint_sha256")
            and observed_identity["fingerprint_sha256"]
            != expected_identity["fingerprint_sha256"]
        ):
            raise VariantConfigurationError(
                "experiment_identity do provider diverge da identidade do runtime."
            )
        trace_comparison = dict(comparison)
        provider_comparison = trace.get("comparison")
        if isinstance(provider_comparison, dict):
            for key in (
                "effective_variant",
                "fallback_stage",
                "fallback_reason",
                "completion_status",
                "fallback",
                "effective_reranker_provider",
                "effective_reranker_model",
                "evidence_window",
            ):
                if key in provider_comparison:
                    trace_comparison[key] = provider_comparison[key]
        provider_reranker = trace.get("reranker") or trace.get("jev_reranker")
        if isinstance(provider_reranker, dict):
            if provider_reranker.get("provider") is not None:
                trace_comparison["effective_reranker_provider"] = str(
                    provider_reranker["provider"]
                )
            if provider_reranker.get("model") is not None:
                trace_comparison["effective_reranker_model"] = str(
                    provider_reranker["model"]
                )
        if "context_envelope" not in trace and isinstance(
            trace.get("context_selection"), dict
        ):
            trace["context_envelope"] = {
                **trace["context_selection"],
                "status": "ready_for_generation",
            }
        latency_ms = int((time.perf_counter() - t0) * 1000)

        evaluation = _evaluate_response(
            case=case,
            answer=answer,
            chunks=chunks,
            trace=trace,
        )

        result = {
            "run_id": run_id,
            "case_id": case_id,
            "question": question,
            "split": str(case.get("split") or "development"),
            "answerability": str(
                case.get("answerability")
                or (
                    "ambiguous"
                    if case.get("expected_behavior") == "clarify"
                    else "no_evidence"
                    if case.get("expected_behavior") == "no_answer"
                    else "answerable"
                )
            ),
            "provenance": case.get("provenance"),
            "review": case.get("review"),
            "variant_id": variant_id,
            "pair_id": pair_id,
            "snapshot_id": snapshot_id,
            "experiment_identity": runtime_metadata.get("experiment_identity"),
            "comparison": trace_comparison,
            **evaluation,
            "top_similarity": rag._safe_similarity(trace.get("top_similarity", 0.0)),
            "latency_ms": latency_ms,
            "trace": _trace_for_report(trace, trace_comparison),
            "answer_preview": (answer or "")[:240],
        }
        if comparison_profile is not None:
            result.pop("question", None)
            result.pop("answer_preview", None)
        result["outcome"] = _case_outcome(result)
        result["effective_variant"] = trace_comparison.get("effective_variant")
        result["fallback"] = bool(
            trace_comparison.get("fallback")
            or trace_comparison.get("fallback_stage")
            or trace_comparison.get("effective_variant") != variant_id
        )
        result["variant_success"] = not result["fallback"] and (
            trace_comparison.get("effective_variant") == variant_id
        )
        result["comparison_outcome"] = (
            "fallback" if result["fallback"] else result["outcome"]
        )
        result["metric_observations"] = {
            name: _metric_observation(name, result) for name in METRIC_DEFINITIONS
        }
        result["completion"] = _paired_completion(
            trace,
            _summarize_model_usage([{"trace": trace}]),
            latency_ms=latency_ms,
        )
        results.append(result)

        rows_to_store.append(
            {
                "run_id": run_id,
                "case_id": case_id,
                "question": question,
                "expected_behavior": evaluation["expected_behavior"],
                "expected_intent": evaluation["expected_intent"],
                "predicted_intent": evaluation["predicted_intent"],
                "abstained": evaluation["abstained"],
                "behavior_match": evaluation["behavior_match"],
                "factual_correctness": evaluation["factual_correctness"],
                "retrieval_relevance": evaluation["retrieval_relevance"],
                "citation_validity": evaluation["citation_validity"],
                "citation_ok": evaluation["citation_ok"],
                "intent_match": evaluation["intent_match"],
                "grounded": evaluation["grounded"],
                "top_similarity": result["top_similarity"],
                "latency_ms": latency_ms,
                "score": evaluation["score"],
                "trace": result["trace"],
                "evaluation_details": {
                    **evaluation["metric_details"],
                    "decision": {
                        "answerability": result["answerability"],
                        "clarified": evaluation["clarified"],
                        "outcome": result["outcome"],
                        "split": result["split"],
                    },
                },
            }
        )

    if not dry_run and rows_to_store:
        rag.supabase_insert("evaluation_results", rows_to_store)

    total = len(results)
    metrics = {
        metric_name: _summarize_metric(results, metric_name)
        for metric_name in METRIC_DEFINITIONS
    }
    grounded_summary = _summarize_metric(results, "grounded")
    scores = [float(result["score"]) for result in results if result["score"] is not None]
    latencies = sorted(int(result["latency_ms"]) for result in results)
    avg_score = round(sum(scores) / len(scores), 4) if scores else None
    avg_latency_ms = round(sum(latencies) / len(latencies), 2) if latencies else None
    p50_latency_ms = _percentile(latencies, 0.50)
    p95_latency_ms = _percentile(latencies, 0.95)
    abstain_rate = (sum(1 for result in results if result["abstained"]) / total) if total else 0.0

    split_summaries = {
        split_name: {
            "sample_size": len(split_results),
            "metrics": {
                metric_name: _summarize_metric(split_results, metric_name)
                for metric_name in METRIC_DEFINITIONS
            },
            "outcomes": _summarize_outcomes(split_results),
        }
        for split_name in ("development", "holdout")
        if (
            split_results := [
                result for result in results if result["split"] == split_name
            ]
        )
    }

    return {
        "run_id": run_id,
        "dataset_name": dataset_name,
        "evaluator_schema_version": EVALUATOR_SCHEMA_VERSION,
        "metric_definitions_version": METRIC_DEFINITIONS_VERSION,
        "metric_definitions_sha256": _canonical_sha256(METRIC_DEFINITIONS),
        "started_at": started_at,
        "total_cases": total,
        "sample": {
            "total": total,
            "by_split": {
                split_name: sum(1 for result in results if result["split"] == split_name)
                for split_name in sorted({result["split"] for result in results})
            },
            "by_answerability": {
                answerability: sum(
                    1 for result in results if result["answerability"] == answerability
                )
                for answerability in ("answerable", "ambiguous", "no_evidence")
            },
            "by_review_status": {
                status: sum(
                    1
                    for result in results
                    if str((result.get("review") or {}).get("status") or "missing")
                    == status
                )
                for status in sorted(
                    {
                        str((result.get("review") or {}).get("status") or "missing")
                        for result in results
                    }
                )
            },
            "human_review_pending": sum(
                1
                for result in results
                if (result.get("review") or {}).get("human_review") == "pending"
            ),
        },
        "runtime": runtime_metadata,
        "runtime_comparison": runtime_comparison,
        "avg_score": avg_score,
        "score_evaluated": len(scores),
        "score_not_evaluated": total - len(scores),
        "avg_latency_ms": avg_latency_ms,
        "p50_latency_ms": p50_latency_ms,
        "p95_latency_ms": p95_latency_ms,
        "grounded_rate": grounded_summary["rate"],
        "citation_ok_rate": metrics["citation_validity"]["rate"],
        "abstain_rate": round(abstain_rate, 4),
        "intent_accuracy": metrics["intent_match"]["rate"],
        "metric_definitions": METRIC_DEFINITIONS,
        "metrics": metrics,
        "outcomes": _summarize_outcomes(results),
        "splits": split_summaries,
        "non_regression": _evaluate_non_regression(
            split_summaries.get("holdout"),
            baseline_config,
            expected_holdout_cases=sum(case.get("split") == "holdout" for case in dataset),
        ),
        "stage_metrics": _summarize_stage_metrics(results),
        "model_usage": _summarize_model_usage(results),
        "limitations": [
            "Os intervalos de 95% usam Wilson e amostras pequenas permanecem incertas.",
            (
                "Correcao factual usa frases pre-registradas; nao mede equivalencia "
                "semantica completa."
            ),
            (
                "unsupported_claims combina fatos proibidos e erros de grounding, "
                "mas nao prova suporte semantico."
            ),
            (
                "LLM-as-judge nao e usado neste baseline; se adicionado, exige versao, "
                "prompt, calibracao humana e medicao de concordancia."
            ),
            (
                "Recall@20 pode ficar limitado pela profundidade retornada pelo pipeline; "
                "retrieved_depth permanece no detalhe de cada caso."
            ),
            (
                "Chamadas sem uso ou preco exposto, inclusive embeddings, tornam "
                "cost_complete falso em vez de serem contabilizadas como custo zero."
            ),
        ],
        "results": results,
    }


def _paired_completion(
    trace: dict[str, Any],
    usage: dict[str, Any],
    *,
    latency_ms: int | float | None = None,
) -> dict[str, Any]:
    calls = trace.get("model_calls")
    external_calls = trace.get("external_calls")
    observed_calls = isinstance(calls, list) and isinstance(external_calls, list)
    completion_status = str(trace.get("completion_status") or "completed")
    latency = latency_ms if latency_ms is not None else trace.get("latency_ms")
    return {
        "status": completion_status,
        "fallback": bool(trace.get("fallback") or trace.get("fallback_used")),
        "calls": {
            "value": usage.get("call_count") if observed_calls else None,
            "denominator": 1 if observed_calls else 0,
            "unavailable_reason": None if observed_calls else "calls_not_observed",
        },
        "tokens": {
            "value": usage.get("totals") if observed_calls else None,
            "denominator": 1 if observed_calls and usage.get("totals") else 0,
            "unavailable_reason": None
            if observed_calls and usage.get("totals")
            else "usage_unavailable",
        },
        "cost": {
            "value": usage.get("estimated_cost_usd") if observed_calls else None,
            "denominator": 1
            if observed_calls and usage.get("cost_complete")
            else 0,
            "unavailable_reason": None
            if observed_calls and usage.get("cost_complete")
            else "cost_incomplete",
        },
        "latency_ms": {
            "value": latency if isinstance(latency, (int, float)) else None,
            "denominator": 1 if isinstance(latency, (int, float)) else 0,
            "unavailable_reason": None
            if isinstance(latency, (int, float))
            else "latency_not_observed",
        },
    }


def _summarize_metric(
    results: list[dict[str, Any]],
    metric_name: str,
) -> dict[str, Any]:
    if metric_name in _CONTINUOUS_METRICS:
        evaluated_values = [
            float(result[metric_name])
            for result in results
            if result.get(metric_name) is not None
        ]
        return {
            "sum": round(sum(evaluated_values), 4),
            "evaluated": len(evaluated_values),
            "not_evaluated": len(results) - len(evaluated_values),
            "rate": (
                round(sum(evaluated_values) / len(evaluated_values), 4)
                if evaluated_values
                else None
            ),
        }
    evaluated_values = [
        bool(result[metric_name])
        for result in results
        if result.get(metric_name) is not None
    ]
    evaluated = len(evaluated_values)
    true_count = sum(evaluated_values)
    if metric_name in _OCCURRENCE_METRICS:
        return {
            "occurrences": true_count,
            "non_occurrences": evaluated - true_count,
            "evaluated": evaluated,
            "not_evaluated": len(results) - evaluated,
            "rate": round(true_count / evaluated, 4) if evaluated else None,
            "confidence_interval": _wilson_interval(true_count, evaluated),
        }
    return {
        "passed": true_count,
        "failed": evaluated - true_count,
        "evaluated": evaluated,
        "not_evaluated": len(results) - evaluated,
        "rate": round(true_count / evaluated, 4) if evaluated else None,
        "confidence_interval": _wilson_interval(true_count, evaluated),
    }


def _summarize_stage_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    metric_names = (
        "recall_at_10",
        "recall_at_20",
        "evidence_discounted_coverage_at_10",
        "ndcg_at_10",
    )
    summary: dict[str, Any] = {}
    for stage_name in RETRIEVAL_STAGE_NAMES:
        stage_entries = [
            result.get("stage_metrics", {}).get(stage_name, {})
            for result in results
        ]
        summary[stage_name] = {
            "available_cases": sum(
                1
                for entry in stage_entries
                if (entry.get("stage") or {}).get("status")
                in {"available", "complete"}
            ),
            "metrics": {
                metric_name: _summarize_metric(
                    [
                        {metric_name: (entry.get("metrics") or {}).get(metric_name)}
                        for entry in stage_entries
                    ],
                    metric_name,
                )
                for metric_name in metric_names
            },
        }
    return summary


def _case_map(summary: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(result.get("case_id")): result
        for result in summary.get("results", [])
        if isinstance(result, dict) and result.get("case_id")
    }


def _stage_ids(result: dict[str, Any], stage_name: str) -> list[str]:
    stage = (result.get("trace") or {}).get("retrieval_stages", {}).get(stage_name, {})
    if not isinstance(stage, dict):
        return []
    values = stage.get("order") or stage.get("ids") or []
    return [str(value) for value in values if str(value).strip()]


def _comparison_stage_ids(trace: dict[str, Any], stage_name: str) -> list[str]:
    stage = (trace or {}).get("retrieval_stages", {}).get(stage_name, {})
    if not isinstance(stage, dict):
        return []
    chunks = stage.get("chunks")
    if isinstance(chunks, list):
        return [_comparison_chunk_id(chunk) for chunk in chunks]
    values = stage.get("order") or stage.get("ids") or []
    return [_opaque_report_id(value, prefix="candidate") for value in values]


def _comparison_chunk_id(chunk: dict[str, Any]) -> str:
    candidate_id = str(chunk.get("candidate_id") or "").strip()
    if candidate_id:
        return _opaque_report_id(candidate_id, prefix="candidate")
    return rag._evaluation_candidate_id(chunk)


def _stage_contract_complete(result: dict[str, Any]) -> bool:
    stages = (result.get("trace") or {}).get("retrieval_stages")
    return isinstance(stages, dict) and all(
        isinstance(stages.get(stage_name), dict)
        for stage_name in RETRIEVAL_STAGE_NAMES
    )


def _paired_view(
    summaries: Mapping[str, dict[str, Any]],
    *,
    view: str,
) -> dict[str, Any]:
    variant_ids = list(summaries)
    if len(variant_ids) < 2:
        return {
            "status": "incomplete",
            "view": view,
            "reason": "at_least_two_variants_required",
            "cases": [],
        }
    reference_id = variant_ids[0]
    reference_cases = _case_map(summaries[reference_id])
    other_cases = {
        variant_id: _case_map(summary)
        for variant_id, summary in summaries.items()
        if variant_id != reference_id
    }
    case_ids = sorted(
        set(reference_cases).intersection(
            *(set(cases) for cases in other_cases.values())
        )
    )
    rows: list[dict[str, Any]] = []
    missing_count = sum(
        len(set(reference_cases) - set(cases))
        for cases in other_cases.values()
    )
    missing_stage_cases = [
        case_id
        for case_id in case_ids
        if any(
            not _stage_contract_complete(_case_map(summaries[variant_id])[case_id])
            for variant_id in variant_ids
        )
    ]
    for case_id in case_ids:
        reference = reference_cases[case_id]
        row: dict[str, Any] = {"case_id": case_id, "variants": {reference_id: {}}}
        for variant_id in variant_ids:
            result = _case_map(summaries[variant_id])[case_id]
            row["variants"][variant_id] = {
                "outcome": result.get("outcome"),
                "score": result.get("score"),
                "effective_variant": (result.get("comparison") or {}).get(
                    "effective_variant"
                ),
                "fallback_reason": (result.get("comparison") or {}).get(
                    "fallback_reason"
                ),
                "candidate_pool_ids": _stage_ids(result, "candidate_pool"),
                "post_rerank_ids": _stage_ids(result, "post_rerank"),
                "final_context_ids": _stage_ids(result, "final_context"),
            }
        row["same_candidate_pool"] = all(
            row["variants"][variant_id]["candidate_pool_ids"]
            == row["variants"][reference_id]["candidate_pool_ids"]
            for variant_id in variant_ids
        )
        row["ranking_order_changed"] = any(
            row["variants"][variant_id]["post_rerank_ids"]
            != row["variants"][reference_id]["post_rerank_ids"]
            for variant_id in variant_ids
            if variant_id != reference_id
        )
        rows.append(row)

    if view == "ranking_ablation_same_pool":
        incompatible = [row["case_id"] for row in rows if not row["same_candidate_pool"]]
        status = (
            "complete"
            if rows and not incompatible and not missing_count and not missing_stage_cases
            else "incomplete"
        )
        return {
            "status": status,
            "view": view,
            "reference_variant": reference_id,
            "case_count": len(rows),
            "missing_result_count": missing_count,
            "missing_stage_contract_cases": missing_stage_cases,
            "incompatible_pool_cases": incompatible,
            "ranking_changed_cases": [
                row["case_id"] for row in rows if row["ranking_order_changed"]
            ],
            "cases": rows,
        }

    fallback_cases = [
        row["case_id"]
        for row in rows
        if any(
            row["variants"][variant_id]["effective_variant"] != variant_id
            for variant_id in variant_ids
        )
    ]
    status = "complete" if rows and not missing_count and not missing_stage_cases else "incomplete"
    return {
        "status": status,
        "view": view,
        "reference_variant": reference_id,
        "case_count": len(rows),
        "missing_result_count": missing_count,
        "missing_stage_contract_cases": missing_stage_cases,
        "fallback_cases": fallback_cases,
        "cases": rows,
    }


def run_paired_comparison(
    *,
    dataset: list[dict[str, Any]],
    dataset_name: str,
    dry_run: bool,
    limit: int | None,
    baseline_config: dict[str, Any],
    split: str = "all",
    variants: list[str] | tuple[str, ...] | None = None,
    answer_providers: Mapping[str, AnswerProvider | None] | None = None,
    pair_id: str | None = None,
    snapshot_id: str | None = None,
) -> dict[str, Any]:
    profile = _comparison_profile(baseline_config)
    configured_variants = [str(value) for value in profile["variants"]]
    selected_variants = list(variants or configured_variants)
    if len(selected_variants) < 2:
        raise VariantConfigurationError("Uma comparação pareada exige pelo menos A e B.")
    if len(set(selected_variants)) != len(selected_variants):
        raise VariantConfigurationError("A comparação não pode repetir variantes.")
    registered_variants = set(configured_variants) | set(
        (profile.get("unavailable_variants") or {}).keys()
    )
    if any(variant not in registered_variants for variant in selected_variants):
        raise VariantConfigurationError(
            "A variante solicitada não está registrada no perfil congelado."
        )
    resolved_pair_id, resolved_snapshot_id = _comparison_identifiers(
        profile,
        pair_id=pair_id,
        snapshot_id=snapshot_id,
        require_snapshot=True,
    )
    providers = dict(answer_providers or {})
    for variant_id in selected_variants:
        _validate_variant_execution(
            variant_id,
            answer_provider=providers.get(variant_id),
            profile=profile,
        )

    selected_cases = _comparison_selected_cases(dataset, split=split, limit=limit)
    case_ids = [str(case.get("id") or "").strip() for case in selected_cases]
    if any(not case_id for case_id in case_ids):
        raise VariantConfigurationError(
            "A comparação pareada exige id não vazio em todos os casos."
        )
    if len(set(case_ids)) != len(case_ids):
        raise VariantConfigurationError(
            "A comparação pareada não pode repetir case_id no recorte selecionado."
        )
    question_to_case = {
        str(case.get("question") or "").strip(): case for case in selected_cases
    }
    if len(question_to_case) != len(selected_cases):
        raise VariantConfigurationError(
            "A comparação pareada exige perguntas únicas no recorte selecionado."
        )

    def _run_mode(mode: str, view: str) -> dict[str, dict[str, Any]]:
        captured_pools: dict[str, list[dict]] = {}
        mode_summaries: dict[str, dict[str, Any]] = {}
        for variant_id in selected_variants:
            provider = providers.get(variant_id)

            def mode_provider(
                question: str,
                scope: dict[str, Any],
                *,
                _provider: ComparisonProvider | None = provider,
                _variant_id: str = variant_id,
            ) -> tuple[str, list[dict], dict[str, Any]]:
                case = question_to_case.get(str(question).strip())
                if case is None:
                    raise VariantConfigurationError(
                        "Provider recebeu pergunta fora do recorte pareado."
                    )
                case_id = str(case.get("id") or "")
                candidate_pool = (
                    captured_pools.get(case_id)
                    if mode == "ranking_ablation" and _variant_id != VARIANT_EXISTING
                    else None
                )
                answer, chunks, trace = _invoke_comparison_provider(
                    _provider,
                    case=case,
                    scope=scope,
                    variant_id=_variant_id,
                    mode=mode,
                    candidate_pool=candidate_pool,
                )
                if _variant_id == VARIANT_EXISTING:
                    pool_chunks = (
                        ((trace.get("retrieval_stages") or {}).get("candidate_pool") or {}).get(
                            "chunks"
                        )
                        if isinstance(trace, dict)
                        else None
                    )
                    if not isinstance(pool_chunks, list):
                        raise VariantConfigurationError(
                            "A ablação pareada exige chunks do candidate_pool no trace da variante existing."
                        )
                    captured_pools[case_id] = list(pool_chunks)
                elif mode == "ranking_ablation":
                    expected_pool = [
                        _comparison_chunk_id(chunk)
                        for chunk in (candidate_pool or [])
                    ]
                    observed_pool = _comparison_stage_ids(
                        trace, "candidate_pool"
                    )
                    if observed_pool != expected_pool:
                        raise VariantConfigurationError(
                            "A variante pareada alterou ou omitiu o candidate_pool da ablação."
                        )
                return answer, chunks, trace

            mode_summaries[variant_id] = run_evaluation(
                dataset=dataset,
                dataset_name=dataset_name,
                dry_run=dry_run,
                limit=limit,
                answer_provider=mode_provider,
                baseline_config=baseline_config,
                split=split,
                variant_id=variant_id,
                pair_id=resolved_pair_id,
                snapshot_id=resolved_snapshot_id,
                comparison_view=view,
                comparison_profile=profile,
            )
        return mode_summaries

    ranking_summaries = _run_mode(
        "ranking_ablation", "ranking_ablation_same_pool"
    )
    end_to_end_summaries = _run_mode(
        "end_to_end", "end_to_end_same_snapshot"
    )
    summaries = end_to_end_summaries

    reference_variant = selected_variants[0]
    unavailable_results = []
    unavailable_declarations = profile.get("unavailable_variants") or {}
    reference_identity = summaries[reference_variant]["runtime"].get(
        "experiment_identity"
    )
    selected_cases = [
        case
        for case in dataset
        if split == "all" or str(case.get("split") or "development") == split
    ]
    if limit and limit > 0:
        selected_cases = selected_cases[:limit]
    for unavailable_id, declaration in unavailable_declarations.items():
        for index, case in enumerate(selected_cases, start=1):
            case_id = str(case.get("id") or f"case-{index:04d}")
            unavailable_results.append(
                {
                    "variant_id": str(unavailable_id),
                    "pair_id": resolved_pair_id,
                    "snapshot_id": resolved_snapshot_id,
                    "experiment_identity": reference_identity,
                    "outcome": "unavailable",
                    "fallback": False,
                    "effective_variant": None,
                    "case_id": case_id,
                    "unavailable_reason": str(declaration.get("reason")),
                }
            )
    identity_comparisons = {
        variant_id: _compare_runtime_identities(
            summaries[variant_id]["runtime"],
            summaries[reference_variant]["runtime"],
            experimental_variables=["comparison.variant_id"],
        )
        for variant_id in selected_variants[1:]
    }
    view_summaries = {
        "ranking_ablation_same_pool": ranking_summaries,
        "end_to_end_same_snapshot": end_to_end_summaries,
    }
    views = {
        view: _paired_view(view_summaries[view], view=view)
        for view in profile.get("views", COMPARISON_VIEWS)
        if view in COMPARISON_VIEWS
    }
    incomplete_identity = any(
        comparison["status"] not in {"compatible", "compatible_with_declared_changes"}
        for comparison in identity_comparisons.values()
    )
    incomplete_views = any(view["status"] != "complete" for view in views.values())
    incomplete_holdout = any(
        isinstance(summary.get("non_regression"), dict)
        and (summary["non_regression"].get("coverage") or {}).get(
            "expected_holdout_cases", 0
        )
        > 0
        and summary["non_regression"].get("status") != "passed"
        for summary in summaries.values()
    )
    return {
        "comparison_schema_version": COMPARISON_SCHEMA_VERSION,
        "evaluator_schema_version": EVALUATOR_SCHEMA_VERSION,
        "dataset_name": dataset_name,
        "pair_id": resolved_pair_id,
        "snapshot_id": resolved_snapshot_id,
        "variants": selected_variants,
        "experiment_identity": {
            variant_id: summaries[variant_id]["runtime"].get("experiment_identity")
            for variant_id in selected_variants
        },
        "unavailable_variants": [
            {
                "variant_id": str(variant_id),
                "status": "unavailable",
                "reason": str(declaration.get("reason")),
            }
            for variant_id, declaration in unavailable_declarations.items()
        ],
        "unavailable_results": unavailable_results,
        "profile": _sanitize_evaluation_metadata(profile),
        "identity_comparisons": identity_comparisons,
        "views": views,
        "status": (
            "incomplete"
            if incomplete_identity or incomplete_views or incomplete_holdout
            else "complete"
        ),
        "approval": {
            "status": "not_evaluated",
            "reason": "A decisão operacional pertence às issues #53, #93 e #96.",
        },
        "summaries": summaries,
        "mode_summaries": view_summaries,
        "limitations": [
            "Fixtures não constituem benchmark operacional nem revisão humana.",
            "Fallback para existing não conta como sucesso da variante Jev.",
            "Custo desconhecido e identidade de banco desconhecida permanecem incompletos.",
        ],
    }


def prepare_comparison(
    *,
    dataset: list[dict[str, Any]],
    dataset_name: str,
    baseline_config: dict[str, Any],
    split: str = "all",
    limit: int | None = None,
    variants: list[str] | tuple[str, ...] | None = None,
    pair_id: str | None = None,
    snapshot_id: str | None = None,
) -> dict[str, Any]:
    profile = _comparison_profile(baseline_config)
    configured_variants = [str(value) for value in profile["variants"]]
    selected_variants = list(variants or configured_variants)
    registered_variants = set(configured_variants) | set(
        (profile.get("unavailable_variants") or {}).keys()
    )
    if any(variant not in registered_variants for variant in selected_variants):
        raise VariantConfigurationError(
            "A variante solicitada não está registrada no perfil congelado."
        )
    resolved_pair_id, resolved_snapshot_id = _comparison_identifiers(
        profile,
        pair_id=pair_id,
        snapshot_id=snapshot_id,
        require_snapshot=False,
    )
    selected = [
        case
        for case in dataset
        if split == "all" or str(case.get("split") or "development") == split
    ]
    if limit and limit > 0:
        selected = selected[:limit]
    blockers = []
    if not resolved_snapshot_id:
        blockers.append("snapshot_id_required_before_execution")
    for variant_id in selected_variants:
        unavailable = (profile.get("unavailable_variants") or {}).get(variant_id)
        definition = _variant_definition(variant_id)
        if unavailable:
            blockers.append(
                f"{variant_id}_unavailable:{unavailable.get('reason') or unavailable.get('unavailable_reason')}"
            )
        elif not definition["available_without_provider"]:
            blockers.append(
                f"{variant_id}_requires_active_path:{','.join(definition['requires'])}"
            )
    canonical_dataset = json.dumps(
        dataset,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    preparation_identity = {
        "schema_version": 1,
        "status": "prepared",
        "dataset_sha256": hashlib.sha256(canonical_dataset).hexdigest(),
        "profile_sha256": _canonical_sha256(profile),
        "pair_id": resolved_pair_id,
        "snapshot_id": resolved_snapshot_id,
        "git_commit": _git_commit(),
        "evaluator_schema_version": EVALUATOR_SCHEMA_VERSION,
        "comparison_schema_version": COMPARISON_SCHEMA_VERSION,
    }
    preparation_identity["fingerprint_sha256"] = _canonical_sha256(
        preparation_identity
    )
    return {
        "comparison_schema_version": COMPARISON_SCHEMA_VERSION,
        "evaluator_schema_version": EVALUATOR_SCHEMA_VERSION,
        "dataset_name": dataset_name,
        "dataset_sha256": _canonical_sha256(dataset),
        "selection": {
            "split": split,
            "limit": limit,
            "case_ids": [str(case.get("id")) for case in selected],
        },
        "pair_id": resolved_pair_id,
        "snapshot_id": resolved_snapshot_id,
        "experiment_identity": preparation_identity,
        "variants": [
            {
                "variant_id": variant_id,
                **_variant_definition(variant_id),
            }
            for variant_id in selected_variants
        ],
        "unavailable_variants": [
            {
                "variant_id": str(variant_id),
                "status": "unavailable",
                "reason": str(
                    definition.get("reason")
                    or definition.get("unavailable_reason")
                ),
            }
            for variant_id, definition in (profile.get("unavailable_variants") or {}).items()
        ],
        "views": profile.get("views", []),
        "external_calls": 0,
        "database_writes": 0,
        "model_calls": 0,
        "status": "blocked" if blockers else "ready",
        "blockers": sorted(set(blockers)),
        "estimated": {
            "paid_calls": "not executed",
            "cost": None,
            "reason": "prepare-only não acessa banco, rede ou providers.",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run offline RAG benchmark and store metrics in Supabase")
    parser.add_argument(
        "--dataset",
        default="evaluation/datasets/maxpedido_eval_dataset.json",
        help="Path to benchmark dataset (JSON array)",
    )
    parser.add_argument(
        "--dataset-name",
        default="maxpedido_baseline_v1",
        help="Logical dataset name stored in evaluation_runs",
    )
    parser.add_argument(
        "--baseline-config",
        default="evaluation/baseline_config.json",
        help="Pre-registered decision policy and non-regression criteria",
    )
    parser.add_argument(
        "--split",
        choices=("all", "development", "holdout"),
        default="all",
        help="Dataset split to execute; holdout must not be used for tuning",
    )
    parser.add_argument(
        "--variant",
        choices=COMPARISON_VARIANTS,
        default=VARIANT_EXISTING,
        help="Variante registrada para uma execução única (default: existing)",
    )
    parser.add_argument(
        "--paired",
        action="store_true",
        help="Executa as variantes registradas como um par comparável",
    )
    parser.add_argument(
        "--pair-id",
        default="",
        help="Identificador estável do par; obrigatório se não estiver no perfil",
    )
    parser.add_argument(
        "--snapshot-id",
        default="",
        help="Snapshot congelado do corpus/feedback usado pelo par",
    )
    parser.add_argument(
        "--prepare-only",
        action="store_true",
        help="Valida o perfil e os arquivos sem acessar DB, rede ou providers",
    )
    parser.add_argument("--limit", type=int, default=0, help="Optional max number of cases")
    parser.add_argument("--dry-run", action="store_true", help="Run without writing to Supabase")
    parser.add_argument(
        "--gate",
        action="store_true",
        help="Exit nonzero unless the complete holdout passes all baseline criteria",
    )
    parser.add_argument(
        "--output-report",
        default="",
        help="Optional output JSON report path",
    )
    parser.add_argument(
        "--compare-report",
        default="",
        help=(
            "Relatorio JSON de referencia. Diferencas nao declaradas ou identidades "
            "desconhecidas produzem saida nao zero depois de salvar o novo relatorio."
        ),
    )
    parser.add_argument(
        "--experimental-variable",
        action="append",
        default=[],
        help=(
            "Caminho em experiment_identity cuja mudanca e deliberada; pode ser repetido. "
            "Exemplo: rag_config.RAG_GLOBAL_CHALLENGER_COUNT."
        ),
    )
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    dataset = _load_dataset(dataset_path)
    baseline_config = _load_json_object(Path(args.baseline_config))
    reference_runtime = None
    if args.compare_report:
        reference_report = _load_json_object(Path(args.compare_report))
        candidate_runtime = reference_report.get("runtime")
        if not isinstance(candidate_runtime, dict):
            raise ValueError("Reference report has no runtime object")
        reference_runtime = candidate_runtime

    if args.prepare_only:
        if args.paired or args.compare_report or args.gate:
            parser.error("--prepare-only não pode ser combinado com --paired, --gate ou --compare-report")
        summary = prepare_comparison(
            dataset=dataset,
            dataset_name=args.dataset_name,
            baseline_config=baseline_config,
            split=args.split,
            limit=args.limit,
            pair_id=args.pair_id or None,
            snapshot_id=args.snapshot_id or None,
        )
    elif args.paired:
        if args.variant != VARIANT_EXISTING:
            parser.error("--paired não aceita --variant; use as variantes registradas no perfil")
        if args.compare_report or args.experimental_variable:
            parser.error("--paired não aceita comparação de runtime legada")
        summary = run_paired_comparison(
            dataset=dataset,
            dataset_name=args.dataset_name,
            dry_run=args.dry_run,
            limit=args.limit,
            baseline_config=baseline_config,
            split=args.split,
            pair_id=args.pair_id or None,
            snapshot_id=args.snapshot_id or None,
        )
    else:
        summary = run_evaluation(
            dataset=dataset,
            dataset_name=args.dataset_name,
            dry_run=args.dry_run,
            limit=args.limit,
            baseline_config=baseline_config,
            split=args.split,
            reference_runtime=reference_runtime,
            experimental_variables=args.experimental_variable,
            variant_id=args.variant,
            pair_id=args.pair_id or None,
            snapshot_id=args.snapshot_id or None,
        )

    report_path = Path(args.output_report) if args.output_report else None
    if report_path is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        report_path = Path(f"evaluation/reports/{stamp}_{args.dataset_name}.json")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    if args.prepare_only:
        print(f"Preparation: {summary['status']}")
        print(f"Dataset: {summary['dataset_name']}")
        variant_names = [
            item.get("variant_id", "") if isinstance(item, dict) else str(item)
            for item in summary["variants"]
        ]
        print(f"Variants: {', '.join(name for name in variant_names if name)}")
        print(f"External calls: {summary['external_calls']}")
        if summary["blockers"]:
            print("Blockers: " + ", ".join(summary["blockers"]))
        print(f"Report: {report_path}")
        return 0 if summary["status"] == "ready" else 1

    if args.paired:
        print(f"Comparison: {summary['status']}")
        print(f"Pair: {summary['pair_id']}")
        print(f"Snapshot: {summary['snapshot_id']}")
        for view_name, view in summary["views"].items():
            print(f"{view_name}: {view['status']}")
        print(f"Report: {report_path}")
        return 0 if summary["status"] == "complete" else 1

    print(f"Run ID: {summary['run_id']}")
    print(f"Dataset: {summary['dataset_name']}")
    print(f"Total cases: {summary['total_cases']}")
    print(f"Average score: {_format_rate(summary['avg_score'], precision=4)}")
    print(
        "Score denominator: "
        f"{summary['score_evaluated']} evaluated, "
        f"{summary['score_not_evaluated']} not evaluated"
    )
    for metric_name, metric in summary["metrics"].items():
        print(
            f"{metric_name}: {_format_rate(metric['rate'])} "
            f"({metric['evaluated']} evaluated, {metric['not_evaluated']} not evaluated)"
        )
    print(f"Grounding trace: {_format_rate(summary['grounded_rate'])}")
    print(f"Abstention rate: {summary['abstain_rate']:.2%}")
    print(
        "Latency: "
        f"avg={summary['avg_latency_ms']} ms, "
        f"p50={summary['p50_latency_ms']} ms, p95={summary['p95_latency_ms']} ms"
    )
    print(
        "Model calls/cost: "
        f"{summary['model_usage']['call_count']} calls, "
        f"cost={summary['model_usage']['estimated_cost_usd']} USD, "
        f"complete={summary['model_usage']['cost_complete']}"
    )
    if summary["non_regression"] is not None:
        print(f"Holdout non-regression: {summary['non_regression']['status']}")
    if summary["runtime_comparison"] is not None:
        print(f"Runtime comparison: {summary['runtime_comparison']['status']}")
    print(f"Report: {report_path}")
    if summary["runtime_comparison"] is not None and not summary["runtime_comparison"]["compatible"]:
        return 1
    if args.gate and (summary["non_regression"] or {}).get("status") != "passed":
        return 1
    return 0


def _format_rate(value: float | None, *, precision: int = 2) -> str:
    if value is None:
        return "not evaluated"
    if precision == 4:
        return f"{value:.4f}"
    return f"{value:.2%}"


if __name__ == "__main__":
    raise SystemExit(main())
