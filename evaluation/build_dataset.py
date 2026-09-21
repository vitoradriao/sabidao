"""
Builds an offline evaluation dataset for maxPedido quality checks.

Sources:
- Seed cases (required)
- Optional top knowledge gaps from Supabase
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import rag
from bot_common import normalize_text


def _load_cases(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8-sig")
    data = json.loads(raw)
    if not isinstance(data, list):
        raise ValueError(f"Dataset file must contain a JSON array: {path}")
    return [item for item in data if isinstance(item, dict)]


def _normalize_case(case: dict[str, Any], idx: int, default_source: str) -> dict[str, Any] | None:
    question = str(case.get("question") or "").strip()
    if not question:
        return None

    expected_behavior = str(case.get("expected_behavior") or "exact_answer").strip().lower()
    if expected_behavior not in {
        "exact_answer",
        "partial_abstain",
        "no_answer",
        "clarify",
    }:
        expected_behavior = "exact_answer"

    expected_intent = str(case.get("expected_intent") or "general").strip().lower()
    if not expected_intent:
        expected_intent = "general"

    scope = case.get("scope") if isinstance(case.get("scope"), dict) else {"level": "global"}
    if not scope:
        scope = {"level": "global"}
    if "level" not in scope:
        scope["level"] = "global"

    normalized = {
        "id": str(case.get("id") or f"case-{idx:04d}"),
        "question": question,
        "expected_behavior": expected_behavior,
        "expected_intent": expected_intent,
        "scope": scope,
        "source": str(case.get("source") or default_source),
    }

    tags = case.get("tags")
    if isinstance(tags, list):
        normalized["tags"] = [str(tag).strip() for tag in tags if str(tag).strip()]

    expected_facts = case.get("expected_facts")
    if isinstance(expected_facts, list):
        normalized["expected_facts"] = expected_facts

    reference_evidence = case.get("reference_evidence")
    if isinstance(reference_evidence, list):
        normalized["reference_evidence"] = reference_evidence

    ranking_judgments = case.get("ranking_judgments")
    if isinstance(ranking_judgments, dict):
        normalized["ranking_judgments"] = ranking_judgments

    forbidden_facts = case.get("forbidden_facts")
    if isinstance(forbidden_facts, list):
        normalized["forbidden_facts"] = forbidden_facts

    split = str(case.get("split") or "development").strip().lower()
    normalized["split"] = split if split in {"development", "holdout"} else "development"

    answerability = str(case.get("answerability") or "").strip().lower()
    if answerability not in {"answerable", "ambiguous", "no_evidence"}:
        answerability = {
            "clarify": "ambiguous",
            "no_answer": "no_evidence",
        }.get(expected_behavior, "answerable")
    normalized["answerability"] = answerability

    for field in ("provenance", "review"):
        value = case.get(field)
        if isinstance(value, dict) or isinstance(value, list):
            normalized[field] = value

    conversation_history = case.get("conversation_history")
    if isinstance(conversation_history, list):
        normalized["conversation_history"] = conversation_history

    return normalized


def _build_gap_cases(limit: int) -> list[dict[str, Any]]:
    if limit <= 0:
        return []

    try:
        gaps = rag.get_top_knowledge_gaps(limit)
    except Exception as exc:
        print(f"WARN: could not load knowledge gaps: {exc}")
        return []

    cases: list[dict[str, Any]] = []
    for idx, gap in enumerate(gaps, start=1):
        query = str(gap.get("query") or "").strip()
        if not query:
            continue
        cases.append(
            {
                "id": f"kgap-{idx:04d}",
                "question": query,
                "expected_behavior": "no_answer",
                "expected_intent": "general",
                "scope": {"level": "global"},
                "source": "knowledge_gap",
                "split": "development",
                "answerability": "no_evidence",
                "provenance": {
                    "kind": "knowledge_gap",
                    "source": "knowledge_gaps",
                },
                "review": {
                    "status": "pending_human",
                    "reviewer": None,
                    "method": "generated_from_runtime_gap",
                },
            }
        )
    return cases


def _dedupe_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()

    for idx, case in enumerate(cases, start=1):
        normalized_case = _normalize_case(case, idx, default_source="merged")
        if normalized_case is None:
            continue

        key = normalize_text(normalized_case["question"])
        if not key or key in seen:
            continue

        seen.add(key)
        deduped.append(normalized_case)

    return deduped


def build_dataset(
    *,
    seed_file: Path,
    output_file: Path,
    gap_limit: int = 0,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []

    seed_cases = _load_cases(seed_file)
    merged.extend(seed_cases)

    merged.extend(_build_gap_cases(gap_limit))

    result = _dedupe_cases(merged)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Build maxPedido evaluation dataset")
    parser.add_argument(
        "--seed-file",
        default="evaluation/datasets/maxpedido_seed_cases.json",
        help="Path to seed cases JSON file",
    )
    parser.add_argument(
        "--output",
        default="evaluation/datasets/maxpedido_eval_dataset.json",
        help="Output dataset JSON file",
    )
    parser.add_argument(
        "--knowledge-gap-limit",
        type=int,
        default=20,
        help="Number of top knowledge gaps to append as no_answer cases",
    )
    args = parser.parse_args()

    dataset = build_dataset(
        seed_file=Path(args.seed_file),
        output_file=Path(args.output),
        gap_limit=max(0, args.knowledge_gap_limit),
    )

    by_behavior: dict[str, int] = {}
    by_source: dict[str, int] = {}
    for item in dataset:
        behavior = str(item.get("expected_behavior") or "unknown")
        source = str(item.get("source") or "unknown")
        by_behavior[behavior] = by_behavior.get(behavior, 0) + 1
        by_source[source] = by_source.get(source, 0) + 1

    print(f"Dataset saved: {args.output}")
    print(f"Total cases: {len(dataset)}")
    print("By behavior:")
    for key in sorted(by_behavior):
        print(f"  - {key}: {by_behavior[key]}")
    print("By source:")
    for key in sorted(by_source):
        print(f"  - {key}: {by_source[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
