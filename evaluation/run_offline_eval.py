"""
Runs offline end-to-end evaluation against the current RAG pipeline and stores results in Supabase.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any, Callable

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
import rag
from bot_common import normalize_text


EVALUATOR_SCHEMA_VERSION = 4
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
    "recall_at_10": "Fracao das evidencias de referencia encontradas ate a posicao 10.",
    "recall_at_20": "Fracao das evidencias de referencia encontradas ate a posicao 20.",
    "ndcg_at_10": (
        "Ganho cumulativo normalizado das evidencias distintas encontradas ate a posicao 10."
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
_CONTINUOUS_METRICS = frozenset({"recall_at_10", "recall_at_20", "ndcg_at_10"})

_CLARIFICATION_RE = re.compile(
    r"\b(?:pode|consegue|preciso|informe|confirme|especifique|qual|quais|onde|quando)\b"
)

_CONFIG_FIELDS = (
    "MAX_CONTEXT_CHUNKS",
    "CHUNK_FETCH_LIMIT",
    "SIMILARITY_THRESHOLD",
    "SIMILARITY_FLOOR_FACTOR",
    "RAG_MIN_STRONG_SIMILARITY",
    "RAG_OPERATIONAL_SIMILARITY_MARGIN",
    "RAG_ENABLE_RERANKING",
    "RERANKER_MIN_TRIGGER_SIM",
    "RERANKER_MAX_TRIGGER_SIM",
    "RERANKER_MAX_CANDIDATES",
    "RAG_ENABLE_GROUNDING_VALIDATION",
    "FULL_CONTEXT_ENABLED",
)

AnswerProvider = Callable[[str, dict[str, Any]], tuple[str, list[dict], dict[str, Any]]]


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
) -> tuple[dict[str, float | None], dict[str, Any]]:
    specs = _evidence_specs(reference_evidence)
    if not specs:
        values = {"recall_at_10": None, "recall_at_20": None, "ndcg_at_10": None}
        return values, {"reason": "missing_reference_evidence", "matched_evidence": {}}

    matched_evidence: dict[int, int] = {}
    gains: list[int] = []
    for rank, chunk in enumerate(chunks or [], start=1):
        rank_gain = 0
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
                rank_gain += 1
        gains.append(rank_gain)

    def recall_at(k: int) -> float:
        found = sum(1 for rank in matched_evidence.values() if rank <= k)
        return round(found / len(specs), 4)

    dcg = sum(gain / math.log2(rank + 1) for rank, gain in enumerate(gains[:10], start=1))
    ideal_count = min(len(specs), 10)
    idcg = sum(1 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
    ndcg = round(dcg / idcg, 4) if idcg else None
    return (
        {
            "recall_at_10": recall_at(10),
            "recall_at_20": recall_at(20),
            "ndcg_at_10": ndcg,
        },
        {
            "reference_count": len(specs),
            "retrieved_depth": len(chunks or []),
            "matched_evidence": {
                str(evidence_index): rank
                for evidence_index, rank in sorted(matched_evidence.items())
            },
        },
    )


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
        "metric_details": {
            "factual_correctness": factual_details,
            "retrieval_relevance": retrieval_details,
            "recall_at_10": ranked_retrieval_details,
            "recall_at_20": ranked_retrieval_details,
            "ndcg_at_10": ranked_retrieval_details,
            "citation_validity": citation_details,
            "false_abstention": {"value": false_abstention},
            "false_absence_claim": {"value": false_absence_claim},
            "unsupported_claims": {
                "value": unsupported_claims,
                "forbidden_fact_matches": forbidden_matches,
                "grounding_errors": list(trace.get("grounding_errors") or []),
            },
        },
    }


def _git_commit() -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        check=False,
    )
    commit = completed.stdout.strip()
    return commit if completed.returncode == 0 and commit else None


def _runtime_metadata(
    dataset: list[dict[str, Any]],
    baseline_config: dict[str, Any] | None,
) -> dict[str, Any]:
    canonical_dataset = json.dumps(
        dataset,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "git_commit": _git_commit(),
        "dataset_sha256": hashlib.sha256(canonical_dataset).hexdigest(),
        "model_config": rag.get_model_config(),
        "embedding_index_identity": rag.get_embedding_index_identity(),
        "rag_config": {
            field: getattr(config, field)
            for field in _CONFIG_FIELDS
            if hasattr(config, field)
        },
        "baseline_config": baseline_config,
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
) -> dict[str, Any]:
    selected = [
        case
        for case in dataset
        if split == "all" or str(case.get("split") or "development") == split
    ]
    selected = selected[:limit] if limit and limit > 0 else selected
    if not selected:
        raise ValueError(f"Dataset has no cases for split: {split}")

    started_at = datetime.now(timezone.utc).isoformat()
    runtime_metadata = _runtime_metadata(dataset, baseline_config)
    runtime_metadata["selection"] = {
        "split": split,
        "limit": limit,
        "case_ids": [
            str(case.get("id") or f"case-{index:04d}")
            for index, case in enumerate(selected, start=1)
        ],
    }
    run_id = "DRY_RUN"
    if not dry_run:
        run_id = _insert_run(
            dataset_name=dataset_name,
            total_cases=len(selected),
            metadata={
                "started_at": started_at,
                "evaluator_schema_version": EVALUATOR_SCHEMA_VERSION,
                **runtime_metadata,
            },
        )

    rows_to_store: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    for index, case in enumerate(selected, start=1):
        case_id = str(case.get("id") or f"case-{index:04d}")
        question = str(case.get("question") or "").strip()
        scope = case.get("scope") if isinstance(case.get("scope"), dict) else {"level": "global"}
        conversation_history = (
            case.get("conversation_history")
            if isinstance(case.get("conversation_history"), list)
            else None
        )

        t0 = time.perf_counter()
        if answer_provider is None:
            answer, chunks, trace = rag.ask(
                question,
                conversation_history=conversation_history,
                images=None,
                system_prompt=None,
                platform="offline_eval",
                scope=scope,
            )
        else:
            answer, chunks, trace = answer_provider(question, scope)
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
            **evaluation,
            "top_similarity": rag._safe_similarity(trace.get("top_similarity", 0.0)),
            "latency_ms": latency_ms,
            "trace": trace,
            "answer_preview": (answer or "")[:240],
        }
        result["outcome"] = _case_outcome(result)
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
                "trace": trace,
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
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    dataset = _load_dataset(dataset_path)
    baseline_config = _load_json_object(Path(args.baseline_config))

    summary = run_evaluation(
        dataset=dataset,
        dataset_name=args.dataset_name,
        dry_run=args.dry_run,
        limit=args.limit,
        baseline_config=baseline_config,
        split=args.split,
    )

    report_path = Path(args.output_report) if args.output_report else None
    if report_path is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        report_path = Path(f"evaluation/reports/{stamp}_{args.dataset_name}.json")

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

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
    print(f"Report: {report_path}")
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
