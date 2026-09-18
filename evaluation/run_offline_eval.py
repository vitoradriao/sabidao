"""
Runs offline end-to-end evaluation against the current RAG pipeline and stores results in Supabase.
"""

from __future__ import annotations

import argparse
import json
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


EVALUATOR_SCHEMA_VERSION = 2
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
    "citation_validity": (
        "Verifica citacao de uma fonte de referencia sem erros de grounding; abstencoes e "
        "casos sem fonte de referencia nao sao avaliados."
    ),
    "intent_match": "Compara a intencao prevista com expected_intent.",
}

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


def _is_abstained(answer: str, trace: dict[str, Any]) -> bool:
    if bool(trace.get("abstained")):
        return True
    return str(answer or "").strip().startswith(config.NO_ANSWER_PHRASE)


def _behavior_match(expected_behavior: str, abstained: bool, answer: str) -> bool:
    expected = (expected_behavior or "exact_answer").strip().lower()
    if expected == "exact_answer":
        return not abstained
    if expected == "no_answer":
        return abstained
    if expected == "partial_abstain":
        # Accept either a strict abstain or a partial response that includes no-answer phrase.
        return abstained or (config.NO_ANSWER_PHRASE in (answer or ""))
    return not abstained


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
    behavior_match = _behavior_match(expected_behavior, abstained, answer)
    factual_correctness, factual_details = _factual_correctness(
        answer,
        abstained,
        case.get("expected_facts"),
    )
    retrieval_relevance, retrieval_details = _retrieval_relevance(
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
        "behavior_match": behavior_match,
        "factual_correctness": factual_correctness,
        "retrieval_relevance": retrieval_relevance,
        "citation_validity": citation_validity,
        "citation_ok": citation_validity,
        "intent_match": intent_match,
        "grounded": grounded,
        "score": score,
        "metric_details": {
            "factual_correctness": factual_details,
            "retrieval_relevance": retrieval_details,
            "citation_validity": citation_details,
        },
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
) -> dict[str, Any]:
    selected = dataset[:limit] if limit and limit > 0 else dataset

    started_at = datetime.now(timezone.utc).isoformat()
    run_id = "DRY_RUN"
    if not dry_run:
        run_id = _insert_run(
            dataset_name=dataset_name,
            total_cases=len(selected),
            metadata={
                "started_at": started_at,
                "evaluator_schema_version": EVALUATOR_SCHEMA_VERSION,
            },
        )

    rows_to_store: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    for index, case in enumerate(selected, start=1):
        case_id = str(case.get("id") or f"case-{index:04d}")
        question = str(case.get("question") or "").strip()
        scope = case.get("scope") if isinstance(case.get("scope"), dict) else {"level": "global"}

        t0 = time.perf_counter()
        if answer_provider is None:
            answer, chunks, trace = rag.ask(
                question,
                conversation_history=None,
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
            **evaluation,
            "top_similarity": rag._safe_similarity(trace.get("top_similarity", 0.0)),
            "latency_ms": latency_ms,
            "trace": trace,
            "answer_preview": (answer or "")[:240],
        }
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
                "evaluation_details": evaluation["metric_details"],
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
    avg_score = round(sum(scores) / len(scores), 4) if scores else None
    abstain_rate = (sum(1 for result in results if result["abstained"]) / total) if total else 0.0

    return {
        "run_id": run_id,
        "dataset_name": dataset_name,
        "evaluator_schema_version": EVALUATOR_SCHEMA_VERSION,
        "total_cases": total,
        "avg_score": avg_score,
        "score_evaluated": len(scores),
        "score_not_evaluated": total - len(scores),
        "grounded_rate": grounded_summary["rate"],
        "citation_ok_rate": metrics["citation_validity"]["rate"],
        "abstain_rate": round(abstain_rate, 4),
        "intent_accuracy": metrics["intent_match"]["rate"],
        "metric_definitions": METRIC_DEFINITIONS,
        "metrics": metrics,
        "results": results,
    }


def _summarize_metric(
    results: list[dict[str, Any]],
    metric_name: str,
) -> dict[str, int | float | None]:
    evaluated_values = [
        bool(result[metric_name])
        for result in results
        if result.get(metric_name) is not None
    ]
    passed = sum(evaluated_values)
    evaluated = len(evaluated_values)
    return {
        "passed": passed,
        "failed": evaluated - passed,
        "evaluated": evaluated,
        "not_evaluated": len(results) - evaluated,
        "rate": round(passed / evaluated, 4) if evaluated else None,
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
        default="maxpedido_offline_eval",
        help="Logical dataset name stored in evaluation_runs",
    )
    parser.add_argument("--limit", type=int, default=0, help="Optional max number of cases")
    parser.add_argument("--dry-run", action="store_true", help="Run without writing to Supabase")
    parser.add_argument(
        "--output-report",
        default="",
        help="Optional output JSON report path",
    )
    args = parser.parse_args()

    dataset_path = Path(args.dataset)
    dataset = _load_dataset(dataset_path)

    summary = run_evaluation(
        dataset=dataset,
        dataset_name=args.dataset_name,
        dry_run=args.dry_run,
        limit=args.limit,
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
    print(f"Report: {report_path}")
    return 0


def _format_rate(value: float | None, *, precision: int = 2) -> str:
    if value is None:
        return "not evaluated"
    if precision == 4:
        return f"{value:.4f}"
    return f"{value:.2%}"


if __name__ == "__main__":
    raise SystemExit(main())
