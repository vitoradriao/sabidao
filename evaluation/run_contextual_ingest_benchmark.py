"""Executa uma variante de ingestao do benchmark de contextualizacao.

O runner existe apenas para o experimento da issue #16. Ele nao muda o fluxo
normal de ``ingest.py`` e nunca inclui nomes, caminhos ou conteudo do corpus no
relatorio gerado.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote, urlparse


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import config
import ingest
import rag
from db import validate_database_config


REPORT_SCHEMA_VERSION = 1
_TOKEN_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_tokens",
)


def _canonical_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _git_commit() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=ROOT_DIR,
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _corpus_identity(directory: Path, *, recursive: bool | None) -> dict[str, Any]:
    root, files = ingest._collect_local_files(
        directory=str(directory),
        create_if_missing=False,
        recursive=recursive,
    )
    if not root.exists():
        raise ValueError("Diretorio do corpus nao existe")
    if not files:
        raise ValueError("Corpus nao contem arquivos suportados")

    manifest = []
    extensions: Counter[str] = Counter()
    total_bytes = 0
    for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
        relative_path = path.relative_to(root).as_posix()
        content = path.read_bytes()
        size = len(content)
        total_bytes += size
        extensions[path.suffix.lower()] += 1
        manifest.append(
            {
                "path_sha256": hashlib.sha256(relative_path.encode("utf-8")).hexdigest(),
                "content_sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": size,
            }
        )

    return {
        "file_count": len(manifest),
        "total_bytes": total_bytes,
        "extensions": dict(sorted(extensions.items())),
        "fingerprint_sha256": _canonical_sha256(manifest),
    }


def _database_identity(label: str) -> dict[str, str]:
    raw_url = str(os.getenv("DATABASE_URL") or "").strip()
    if not raw_url:
        raise ValueError("DATABASE_URL nao configurada")
    parsed = urlparse(raw_url)
    database = unquote(parsed.path.lstrip("/"))
    if (
        parsed.scheme not in {"postgres", "postgresql"}
        or not parsed.hostname
        or not database
    ):
        raise ValueError("DATABASE_URL deve identificar host e banco PostgreSQL")
    target = {
        "host": parsed.hostname.lower(),
        "port": parsed.port or 5432,
        "database": database,
    }
    return {
        "operator_label": label,
        "target_sha256": _canonical_sha256(target),
    }


def _effective_config(variant: str) -> dict[str, Any]:
    model_config = rag.get_model_config()
    contextualization_enabled = config.CONTEXTUAL_RETRIEVAL_ENABLED
    try:
        config.CONTEXTUAL_RETRIEVAL_ENABLED = True
        contextual_contract = ingest._contextual_retrieval_identity()
    finally:
        config.CONTEXTUAL_RETRIEVAL_ENABLED = contextualization_enabled
    contextual_contract.pop("enabled", None)
    values = {
        "variant": variant,
        "contextual_retrieval_enabled": contextualization_enabled,
        "contextual_retrieval": ingest._contextual_retrieval_identity(),
        "contextual_contract": contextual_contract,
        "contextual_batch_size": config.CONTEXTUAL_RETRIEVAL_BATCH_SIZE,
        "contextual_max_doc_chars": config.CONTEXTUAL_RETRIEVAL_MAX_DOC_CHARS,
        "contextual_max_tokens": config.CONTEXTUAL_RETRIEVAL_MAX_TOKENS,
        "chunk_size": config.CHUNK_SIZE,
        "chunk_overlap": config.CHUNK_OVERLAP,
        "analytical_context_enabled": config.ANALYTICAL_CONTEXT_ENABLED,
        "embedding_batch_size": config.EMBEDDING_BATCH_SIZE,
        "embedding_index": rag.get_embedding_index_identity(),
        "models": model_config,
    }
    invariant_values = {
        key: value
        for key, value in values.items()
        if key not in {"variant", "contextual_retrieval_enabled", "contextual_retrieval"}
    }
    values["invariants_fingerprint_sha256"] = _canonical_sha256(invariant_values)
    return values


def _known_zero_model_usage() -> dict[str, Any]:
    return {
        "pricing_version": getattr(rag, "_MODEL_PRICING_VERSION", "unknown"),
        "call_count": 0,
        "calls_with_usage": 0,
        "calls_without_usage": 0,
        "usage_complete": True,
        "totals": {field: 0 for field in _TOKEN_FIELDS},
        "totals_complete": {field: True for field in _TOKEN_FIELDS},
        "known_estimated_cost_usd": 0.0,
        "estimated_cost_usd": 0.0,
        "cost_complete": True,
        "by_stage": {},
    }


class _Telemetry:
    def __init__(self) -> None:
        self.model_calls: list[dict[str, Any]] = []
        self.embedding_calls: list[dict[str, Any]] = []
        self.contextual_batches = 0
        self.contextual_chunks_requested = 0
        self.contextual_chunks_applied = 0

    def summary(self, *, contextualization_enabled: bool) -> dict[str, Any]:
        contextual = rag._summarize_model_calls(self.model_calls)
        if not contextualization_enabled and not self.model_calls:
            contextual = _known_zero_model_usage()

        embedding_count = len(self.embedding_calls)
        embedding = {
            "call_count": embedding_count,
            "input_count": sum(call["input_count"] for call in self.embedding_calls),
            "input_characters": sum(
                call["input_characters"] for call in self.embedding_calls
            ),
            "totals": {field: None for field in _TOKEN_FIELDS},
            "usage_complete": embedding_count == 0,
            "known_estimated_cost_usd": None,
            "estimated_cost_usd": 0.0 if embedding_count == 0 else None,
            "cost_complete": embedding_count == 0,
            "limitation": (
                None
                if embedding_count == 0
                else "O cliente de embeddings nao expoe uso ao runner; tokens e custo permanecem desconhecidos."
            ),
            "calls": self.embedding_calls,
        }

        total_cost_complete = bool(
            contextual["cost_complete"] and embedding["cost_complete"]
        )
        known_costs = [
            value
            for value in (
                contextual.get("known_estimated_cost_usd"),
                embedding.get("known_estimated_cost_usd"),
            )
            if value is not None
        ]
        return {
            "contextualization": contextual,
            "embeddings": embedding,
            "combined": {
                "call_count": contextual["call_count"] + embedding_count,
                "tokens_complete": bool(
                    contextual["usage_complete"] and embedding["usage_complete"]
                ),
                "known_estimated_cost_usd": (
                    round(sum(known_costs), 12) if known_costs else None
                ),
                "estimated_cost_usd": (
                    round(sum(known_costs), 12) if total_cost_complete else None
                ),
                "cost_complete": total_cost_complete,
            },
            "contextualization_batches": self.contextual_batches,
            "contextual_chunks_requested": self.contextual_chunks_requested,
            "contextual_chunks_applied": self.contextual_chunks_applied,
        }


@contextmanager
def _capture_telemetry(telemetry: _Telemetry) -> Iterator[None]:
    original_generate = rag._gemini_generate
    original_embeddings = ingest.create_document_embeddings
    original_contextualize = ingest._contextualize_chunks_batch

    def generate_with_usage(*args, **kwargs):
        options = dict(kwargs)
        options["stage"] = "ingest_contextualization"
        options["model_calls"] = telemetry.model_calls
        options["routing_reason"] = "contextual_ingest_benchmark"
        return original_generate(*args, **options)

    def embeddings_with_usage(contents: list[str]):
        identity = rag.get_embedding_index_identity()
        started_at = time.perf_counter()
        call = {
            "stage": "document_embedding",
            "provider": identity["provider"],
            "model": identity["model"],
            "input_count": len(contents),
            "input_characters": sum(len(content) for content in contents),
            "usage": None,
            "estimated_cost_usd": None,
            "cost_status": "usage_unavailable",
        }
        try:
            vectors = original_embeddings(contents)
        except Exception as exc:
            call["status"] = "error"
            call["error_type"] = type(exc).__name__
            raise
        else:
            call["status"] = "success"
            call["error_type"] = None
            return vectors
        finally:
            call["latency_ms"] = int((time.perf_counter() - started_at) * 1000)
            telemetry.embedding_calls.append(call)

    def contextualize_with_counts(**kwargs):
        pairs = kwargs.get("chunks_with_indices") or []
        enabled = bool(config.CONTEXTUAL_RETRIEVAL_ENABLED and pairs)
        if enabled:
            telemetry.contextual_batches += 1
            telemetry.contextual_chunks_requested += len(pairs)
        result = original_contextualize(**kwargs)
        if enabled:
            original_by_index = dict(pairs)
            telemetry.contextual_chunks_applied += sum(
                1
                for chunk_index, value in result
                if value != original_by_index.get(chunk_index)
            )
        return result

    rag._gemini_generate = generate_with_usage
    ingest.create_document_embeddings = embeddings_with_usage
    ingest._contextualize_chunks_batch = contextualize_with_counts
    try:
        yield
    finally:
        rag._gemini_generate = original_generate
        ingest.create_document_embeddings = original_embeddings
        ingest._contextualize_chunks_batch = original_contextualize


def _result_summary(
    results: list[dict[str, Any]],
    *,
    expected_documents: int,
) -> dict[str, Any]:
    error_types: Counter[str] = Counter()
    for result in results:
        if result.get("error"):
            error_types[str(result.get("error_type") or "ingestion_error")] += 1
    documents_succeeded = sum(
        1 for result in results if not result.get("error") and not result.get("skipped")
    )
    documents_skipped = sum(1 for result in results if result.get("skipped"))
    documents_failed = sum(1 for result in results if result.get("error"))
    failed_chunks = sum(int(result.get("failed_chunks") or 0) for result in results)
    complete = bool(
        documents_succeeded == expected_documents
        and len(results) == expected_documents
        and documents_skipped == 0
        and documents_failed == 0
        and failed_chunks == 0
    )
    return {
        "status": "complete" if complete else "incomplete",
        "complete": complete,
        "documents_expected": expected_documents,
        "documents_returned": len(results),
        "documents_succeeded": documents_succeeded,
        "documents_skipped": documents_skipped,
        "documents_failed": documents_failed,
        "chunks_processed": sum(int(result.get("chunks_count") or 0) for result in results),
        "failed_chunks": failed_chunks,
        "error_types": dict(sorted(error_types.items())),
    }


def _load_reference(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _validate_reference(current: dict[str, Any], reference: dict[str, Any]) -> None:
    if reference.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise ValueError("Relatorio de referencia usa schema incompativel")
    if reference.get("report_type") != "contextual_ingest_benchmark":
        raise ValueError("Arquivo de referencia nao e um benchmark contextual")
    if current.get("variant") != "llm" or reference.get("variant") != "deterministic":
        raise ValueError("A comparacao exige referencia deterministic e variante llm")
    checks = (
        ("git_commit", current["git_commit"], reference.get("git_commit")),
        (
            "corpus",
            current["corpus"]["fingerprint_sha256"],
            (reference.get("corpus") or {}).get("fingerprint_sha256"),
        ),
        (
            "configuracao invariavel",
            current["configuration"]["invariants_fingerprint_sha256"],
            (reference.get("configuration") or {}).get(
                "invariants_fingerprint_sha256"
            ),
        ),
    )
    mismatches = [label for label, left, right in checks if left != right]
    if mismatches:
        raise ValueError(
            "Relatorio de referencia incompativel: " + ", ".join(mismatches)
        )
    reference_database = (reference.get("database") or {}).get("target_sha256")
    if not reference_database:
        raise ValueError("Relatorio de referencia nao identifica o banco")
    if current["database"]["target_sha256"] == reference_database:
        raise ValueError("As variantes devem usar bancos isolados diferentes")


def run_benchmark(
    *,
    directory: Path,
    variant: str,
    database_label: str,
    recursive: bool | None,
    reference_report: Path | None,
) -> dict[str, Any]:
    if variant not in {"deterministic", "llm"}:
        raise ValueError("Variante de benchmark invalida")
    if variant == "llm" and reference_report is None:
        raise ValueError("A variante llm exige --reference-report deterministic")
    if variant == "deterministic" and reference_report is not None:
        raise ValueError("A variante deterministic nao aceita --reference-report")

    contextualization_enabled = variant == "llm"
    previous_contextualization = config.CONTEXTUAL_RETRIEVAL_ENABLED
    config.CONTEXTUAL_RETRIEVAL_ENABLED = contextualization_enabled
    try:
        identity = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "report_type": "contextual_ingest_benchmark",
            "variant": variant,
            "git_commit": _git_commit(),
            "database": _database_identity(database_label),
            "corpus": _corpus_identity(directory, recursive=recursive),
            "configuration": _effective_config(variant),
        }
        if reference_report is not None:
            _validate_reference(identity, _load_reference(reference_report))

        telemetry = _Telemetry()
        started_at = datetime.now(timezone.utc)
        started_clock = time.perf_counter()
        with _capture_telemetry(telemetry):
            results = ingest.ingest_directory(
                directory=str(directory),
                force=True,
                recursive=recursive,
            )
        finished_at = datetime.now(timezone.utc)
        ingestion = _result_summary(
            results,
            expected_documents=identity["corpus"]["file_count"],
        )

        return {
            **identity,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_ms": int((time.perf_counter() - started_clock) * 1000),
            "ingestion": ingestion,
            "provider_usage": telemetry.summary(
                contextualization_enabled=contextualization_enabled
            ),
            "limitations": [
                "O relatorio nao inclui nomes, caminhos, URLs nem conteudo do corpus.",
                "Tokens e custo de embeddings permanecem desconhecidos quando o cliente nao expoe uso.",
                "O custo e estimado pela tabela versionada do repositorio, nao por uma fatura do provider.",
            ],
        }
    finally:
        config.CONTEXTUAL_RETRIEVAL_ENABLED = previous_contextualization


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Mede a ingestao de uma variante do benchmark contextual da issue #16"
    )
    parser.add_argument("directory", type=Path, help="Corpus autorizado para o experimento")
    parser.add_argument(
        "--variant",
        required=True,
        choices=("deterministic", "llm"),
    )
    parser.add_argument("--database-label", required=True)
    parser.add_argument(
        "--confirm-isolated-database",
        action="store_true",
        help="Confirma que DATABASE_URL aponta para banco exclusivo desta variante",
    )
    parser.add_argument("--reference-report", type=Path)
    parser.add_argument("--output-report", type=Path, required=True)
    parser.add_argument("--recursive", dest="recursive", action="store_true")
    parser.add_argument("--no-recursive", dest="recursive", action="store_false")
    parser.set_defaults(recursive=None)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if not args.confirm_isolated_database:
        raise SystemExit(
            "Recusado: use --confirm-isolated-database somente apos verificar DATABASE_URL."
        )
    if (
        args.reference_report is not None
        and args.output_report.resolve() == args.reference_report.resolve()
    ):
        raise SystemExit(
            "Recusado: o relatorio de saida nao pode sobrescrever a referencia."
        )

    config.validate_ai_config()
    validate_database_config()
    report = run_benchmark(
        directory=args.directory.resolve(),
        variant=args.variant,
        database_label=args.database_label,
        recursive=args.recursive,
        reference_report=args.reference_report,
    )
    args.output_report.parent.mkdir(parents=True, exist_ok=True)
    with args.output_report.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"Relatorio sanitizado salvo em {args.output_report}")
    return 0 if report["ingestion"]["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
