"""Gate offline para comparar relatórios de um lote de migração documental.

Este módulo consome relatórios já produzidos. Não executa RAG, SQL ou providers.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from evaluation.run_offline_eval import _compare_runtime_identities


ROLES = {"identifier", "paraphrase", "boundary"}
ANSWERABILITY = {"answerable", "ambiguous", "no_evidence"}


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else None


def _nested(payload: dict[str, Any], path: str) -> Any:
    value: Any = payload
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _human_reviewed(value: Any) -> bool:
    if not isinstance(value, dict) or value.get("human_review") != "approved":
        return False
    reviewer = value.get("human_reviewer")
    reviewed_at = value.get("human_reviewed_at")
    if not isinstance(reviewer, str) or not reviewer.strip() or not isinstance(reviewed_at, str):
        return False
    try:
        date.fromisoformat(reviewed_at)
    except ValueError:
        try:
            datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
        except ValueError:
            return False
    return isinstance(value.get("divergences"), list)


def evaluate_migration_batch(
    preservation: dict[str, Any],
    reference: dict[str, Any],
    candidate: dict[str, Any],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Retorna passed/failed/incomplete sem tratar evidência ausente como sucesso."""
    checks: list[dict[str, Any]] = []

    def record(name: str, passed: bool | None, reason: str | None = None) -> None:
        checks.append({
            "id": name,
            "status": "passed" if passed is True else "failed" if passed is False else "incomplete",
            "reason": reason,
        })

    batch_id = str(policy.get("batch_id") or "")
    record("policy_version", True if policy.get("schema_version") == 1 else None)
    record(
        "batch_identity",
        True if batch_id and batch_id == preservation.get("batch_id") else
        False if batch_id and preservation.get("batch_id") else None,
        "batch_id ausente ou divergente",
    )
    preservation_status = preservation.get("status")
    coverage = preservation.get("coverage") or {}
    losses = preservation.get("critical_losses") or []
    if preservation_status == "failed" or losses:
        record("preservation", False, "perda estrutural ou critica")
    elif preservation_status == "passed" and coverage.get("mapped") == coverage.get("total") and coverage.get("total", 0) > 0:
        record("preservation", True)
    else:
        record("preservation", None, "mapa ou revisão de preservação incompletos")
    record(
        "source_review",
        True if preservation.get("review_status") == "reviewed" else None,
        "revisão humana das unidades pendente",
    )

    units = preservation.get("unit_results") or []
    record(
        "unit_preservation_status",
        False if any(unit.get("status") == "failed" for unit in units if isinstance(unit, dict)) else
        True if units and all(unit.get("status") == "passed" for unit in units if isinstance(unit, dict)) else None,
        "estado de preservação de unidade indisponível ou com falha",
    )
    unit_ids = {
        str(unit.get("unit_id"))
        for unit in units
        if isinstance(unit, dict) and unit.get("unit_id")
    }
    reference_rows = reference.get("results") or []
    candidate_rows = candidate.get("results") or []
    reference_by_id = {
        str(row.get("case_id")): row
        for row in reference_rows
        if isinstance(row, dict) and row.get("case_id")
    }
    candidate_by_id = {
        str(row.get("case_id")): row
        for row in candidate_rows
        if isinstance(row, dict) and row.get("case_id")
    }
    same_cases = (
        bool(reference_by_id)
        and len(reference_by_id) == len(reference_rows)
        and len(candidate_by_id) == len(candidate_rows)
        and reference_by_id.keys() == candidate_by_id.keys()
        and all(
            reference_by_id[case_id].get("question") == row.get("question")
            and reference_by_id[case_id].get("answerability") == row.get("answerability")
            for case_id, row in candidate_by_id.items()
        )
    )
    record("paired_cases", True if same_cases else None, "casos/perguntas diferem ou execução parcial")

    by_unit: dict[str, list[dict[str, Any]]] = {unit_id: [] for unit_id in unit_ids}
    if same_cases:
        for row in candidate_rows:
            migration = ((row.get("provenance") or {}).get("migration") or {})
            unit_id = str(migration.get("unit_id") or "")
            if unit_id in by_unit and migration.get("batch_id") == batch_id:
                by_unit[unit_id].append(row)
    migration_rows = [row for rows in by_unit.values() for row in rows]
    coverage_ok = bool(by_unit) and len(migration_rows) == len(candidate_rows) and all(
        len(rows) >= 3
        and ROLES <= {
            str(((row.get("provenance") or {}).get("migration") or {}).get("role"))
            for row in rows
        }
        for rows in by_unit.values()
    )
    record("queries_per_unit", True if coverage_ok else None, "cada unidade precisa de identificador, paráfrase e limite")
    positions = {
        ((row.get("provenance") or {}).get("migration") or {}).get("position")
        for row in migration_rows
    }
    answerability = {row.get("answerability") for row in migration_rows}
    critical = any(
        ((row.get("provenance") or {}).get("migration") or {}).get("critical") is True
        for row in migration_rows
    )
    record(
        "critical_and_boundary_cases",
        True if critical and {"middle", "end"} <= positions and ANSWERABILITY <= answerability else None,
        "faltam assunto crítico, meio/fim, ambiguidade ou abstenção",
    )
    review_ok = bool(migration_rows) and all(
        _human_reviewed(row.get("review"))
        for row in migration_rows
    )
    record("query_review", True if review_ok else None, "revisão humana dos casos pendente")

    incomplete_evidence = False
    wrong_evidence = False
    wrong_behavior = False
    invalid_citation = False
    false_absence = False
    unknown_absence = False
    for row in migration_rows:
        kind = row.get("answerability")
        if kind == "answerable":
            if row.get("abstained") is True or row.get("clarified") is True:
                wrong_behavior = True
            recall = _number(row.get("recall_at_20"))
            if recall is None:
                incomplete_evidence = True
            elif recall < 1.0:
                wrong_evidence = True
            if row.get("citation_validity") is None:
                incomplete_evidence = True
        elif kind == "ambiguous" and row.get("clarified") is not True:
            wrong_behavior = True
        elif kind == "no_evidence" and row.get("abstained") is not True:
            wrong_behavior = True
        if row.get("citation_validity") is False:
            invalid_citation = True
        if row.get("false_absence_claim") is True:
            false_absence = True
        elif kind == "answerable" and row.get("false_absence_claim") is None:
            unknown_absence = True
    record("top20_evidence", False if wrong_evidence else None if incomplete_evidence or not migration_rows else True)
    record("ambiguity_and_abstention", False if wrong_behavior else True if migration_rows else None)
    record("citations", False if invalid_citation else True if migration_rows else None)
    record("false_absence", False if false_absence else None if unknown_absence or not migration_rows else True)

    frozen_at = _timestamp(policy.get("frozen_at"))
    reference_at = _timestamp(reference.get("started_at"))
    candidate_at = _timestamp(candidate.get("started_at"))
    record(
        "policy_frozen_before_trials",
        True if frozen_at and reference_at and candidate_at and frozen_at <= min(reference_at, candidate_at) else None,
        "limites não congelados antes das execuções",
    )
    environment = policy.get("environment") or {}
    original_snapshot = preservation.get("original_snapshot") or {}
    record(
        "source_snapshot_identity",
        True if original_snapshot.get("sha256") and environment.get("original_snapshot_sha256") == original_snapshot.get("sha256") else
        False if original_snapshot.get("sha256") and environment.get("original_snapshot_sha256") else None,
        "snapshot original ausente ou divergente da política",
    )
    reference_corpus = _nested(reference, "runtime.experiment_identity.database.corpus.sha256")
    candidate_corpus = _nested(candidate, "runtime.experiment_identity.database.corpus.sha256")
    isolated = (
        environment.get("disposable_database") is True
        and environment.get("reference_snapshot")
        and environment.get("candidate_snapshot")
        and environment["reference_snapshot"] != environment["candidate_snapshot"]
        and environment.get("reference_corpus_sha256") == reference_corpus
        and environment.get("candidate_corpus_sha256") == candidate_corpus
        and reference_corpus != candidate_corpus
    )
    record("isolated_snapshots", True if isolated else None, "snapshots isolados e hashes de corpus não comprovados")

    declared = policy.get("declared_changes") or []
    comparison = _compare_runtime_identities(
        candidate.get("runtime") or {},
        reference.get("runtime") or {},
        experimental_variables=declared if isinstance(declared, list) else [],
    )
    record(
        "experiment_identity",
        True if comparison["compatible"] and "database.corpus.sha256" in declared else
        False if comparison["status"] == "incompatible" else None,
        comparison["status"],
    )

    limits = policy.get("limits") or {}
    observed = {
        "max_total_tokens": _nested(candidate, "model_usage.totals.total_tokens"),
        "max_p95_latency_ms": candidate.get("p95_latency_ms"),
        "max_cost_usd": _nested(candidate, "model_usage.estimated_cost_usd"),
    }
    for name, actual in observed.items():
        ceiling = _number(limits.get(name))
        measured = _number(actual)
        if name == "max_cost_usd" and _nested(candidate, "model_usage.cost_complete") is not True:
            measured = None
        record(name, measured <= ceiling if measured is not None and ceiling is not None and ceiling >= 0 else None,
               "limite ou medição indisponível")

    aggregate_rules = policy.get("non_regression_metrics")
    if not isinstance(aggregate_rules, list) or not aggregate_rules:
        record("aggregate_non_regression", None, "métricas agregadas não congeladas")
    else:
        unavailable = False
        regression = False
        for rule in aggregate_rules:
            if not isinstance(rule, dict):
                unavailable = True
                continue
            path = rule.get("path")
            direction = rule.get("direction")
            if not isinstance(path, str) or not path.startswith(("metrics.", "outcomes.")) or direction not in {"higher", "lower"}:
                unavailable = True
                continue
            before = _number(_nested(reference, path))
            after = _number(_nested(candidate, path))
            if before is None or after is None:
                unavailable = True
            elif (direction == "higher" and after < before) or (direction == "lower" and after > before):
                regression = True
        record("aggregate_non_regression", False if regression else None if unavailable else True)

    status = (
        "failed" if any(check["status"] == "failed" for check in checks)
        else "incomplete" if any(check["status"] == "incomplete" for check in checks)
        else "passed"
    )
    return {
        "schema_version": 1,
        "batch_id": batch_id,
        "status": status,
        "checks": checks,
        "case_count": len(candidate_by_id),
        "unit_count": len(unit_ids),
        "runtime_comparison_status": comparison["status"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Gate offline de um lote documental")
    parser.add_argument("--preservation-report", type=Path, required=True)
    parser.add_argument("--reference-report", type=Path, required=True)
    parser.add_argument("--candidate-report", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payloads = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (
            args.preservation_report,
            args.reference_report,
            args.candidate_report,
            args.policy,
        )
    ]
    result = evaluate_migration_batch(*payloads)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Lote {result['batch_id']}: {result['status']}")
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
