"""
ingest.py - Ingestao de documentos: le arquivos, faz chunking (MARKDOWN),
gera embeddings e salva no PostgreSQL.

PDFs sao convertidos para Markdown externamente via DeepSeek.
Para PDFs nao-convertidos, usa PyPDF2 como fallback basico.

Uso:
    py ingest.py                 # ingere apenas arquivos novos
    py ingest.py --force         # re-ingere todos os arquivos
    py ingest.py /caminho/docs   # diretorio customizado
"""

import argparse
import hashlib
import io
import ipaddress
import json
import logging
import random
import re
import socket
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from uuid import uuid4

import httpx
from langchain_text_splitters import MarkdownTextSplitter

import config
from bot_common import normalize_text
from db import (
    db_advisory_xact_lock,
    db_delete,
    db_insert,
    db_select,
    db_transaction,
    db_update,
    validate_database_config,
)
from rag import (
    QUERY_MODULE_HINTS,
    create_document_embeddings,
    embedding_to_pgvector,
    ensure_embedding_index_identity,
    get_model_config,
)

logger = logging.getLogger(__name__)
# O logger do httpx inclui a URL completa, que pode conter query strings sensiveis.
logging.getLogger("httpx").setLevel(logging.WARNING)
_last_embed_call_at = 0.0
_http_client: httpx.Client | None = None
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})


class WebFetchError(ValueError):
    """Erro seguro de coleta web, sem incluir a URL ou o corpo remoto."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def supabase_delete(table: str, column: str, value: str, *, connection=None) -> None:
    db_delete(table, {column: f"eq.{value}"}, connection=connection)


def supabase_insert(table: str, data: dict | list, *, connection=None) -> list:
    return db_insert(table, data, connection=connection)


def supabase_select(table: str, select: str = "*", filters: dict | None = None, *, connection=None) -> list:
    return db_select(table, columns=select, filters=filters, connection=connection)


def supabase_update(table: str, data: dict, filters: dict, *, connection=None) -> list:
    return db_update(table, data, filters, connection=connection)

# --- NOVAS FUNÇÕES DE LEITURA ---

def read_txt(filepath: str) -> str:
    with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()

def read_pdf(filepath: str) -> str:
    """
    Le PDF usando PyPDF2 (fallback basico).
    Para melhor qualidade, converta PDFs para Markdown via DeepSeek
    e coloque o .md na pasta de documentos.
    """
    from PyPDF2 import PdfReader

    logger.info("Processando PDF com PyPDF2: %s", Path(filepath).name)
    logger.info(
        "Dica: para melhor qualidade, converta o PDF para .md via DeepSeek "
        "e coloque na pasta de documentos."
    )

    try:
        reader = PdfReader(filepath)
        pages = []
        for page in reader.pages:
            text = page.extract_text()
            if text and text.strip():
                pages.append(text.strip())

        full_text = "\n\n".join(pages)

        if not full_text.strip():
            logger.warning("PyPDF2 retornou vazio para %s", filepath)

        return full_text

    except Exception as e:
        logger.error("Erro ao ler PDF: %s", e)
        raise


def read_pdf_bytes(data: bytes) -> str:
    """
    Le PDF a partir de bytes usando PyPDF2.
    """
    from PyPDF2 import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = []
    for page in reader.pages:
        text = page.extract_text()
        if text and text.strip():
            pages.append(text.strip())
    return "\n\n".join(pages)

def read_docx(filepath: str) -> str:
    from docx import Document
    doc = Document(filepath)
    # Tenta manter uma estrutura minima de paragrafos
    return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())


READERS = {
    ".txt": read_txt,
    ".md": read_txt,
    ".csv": read_txt,
    ".json": read_txt,
    ".py": read_txt,
    ".js": read_txt,
    ".html": read_txt,
    ".xml": read_txt,
    ".pdf": read_pdf,   # PyPDF2 (fallback — prefira converter PDF → .md via DeepSeek)
    ".docx": read_docx,
}

# --- CHUNKING (singleton lazy do splitter) ---

_splitter: MarkdownTextSplitter | None = None
_document_sections_available: bool | None = None
_INGEST_PROCESSING_VERSION = "ingest-v1"


@dataclass
class AnalyticalSection:
    section_index: int
    title: str
    heading_path: str
    content: str
    module: str
    answer_mode: str
    entities: dict[str, list[str]]
    semantic_context: str
    section_id: str | None = None


def _entities_as_lines(entities: dict[str, list[str]] | None) -> list[str]:
    if not isinstance(entities, dict):
        return []
    labels = {
        "tables": "Tabelas",
        "parameters": "Parametros",
        "endpoints": "Endpoints",
        "statuses": "Status",
        "error_terms": "Termos",
    }
    lines: list[str] = []
    for key, label in labels.items():
        values = entities.get(key) or []
        clean_values = [str(value).strip() for value in values if str(value).strip()]
        if clean_values:
            lines.append(f"{label}: {', '.join(clean_values[:8])}")
    return lines


def _build_section_retrieval_text(doc_title: str, section: AnalyticalSection) -> str:
    excerpt = re.sub(r"\s+", " ", section.content or "").strip()
    if len(excerpt) > 900:
        excerpt = excerpt[:900].rsplit(" ", 1)[0].strip() + "..."

    parts = [
        f"Documento: {doc_title}",
        f"Secao: {section.heading_path or section.title}",
        f"Modulo: {section.module}",
        f"Modo de resposta: {section.answer_mode}",
    ]
    semantic_context = str(section.semantic_context or "").strip()
    if semantic_context:
        parts.append(semantic_context)
    parts.extend(_entities_as_lines(section.entities))
    if excerpt:
        parts.append(f"Trecho da secao: {excerpt}")
    return "\n".join(part for part in parts if part)


def _content_hash(text: str) -> str:
    """Identifica o texto extraido sem misturar caminho ou proveniencia."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _processing_hash(
    *,
    content_hash: str,
    title: str,
    doc_type: str,
    module: str,
    doc_priority: int,
    sections: list[AnalyticalSection],
    chunk_items: list[tuple[int, str, str, AnalyticalSection]],
) -> str:
    """Identifica todos os insumos que alteram chunks ou embeddings."""
    payload = {
        "version": _INGEST_PROCESSING_VERSION,
        "content_hash": content_hash,
        "doc_type": doc_type,
        "module": module,
        "doc_priority": doc_priority,
        "embedding": {
            "provider": config.EMBEDDING_PROVIDER,
            "model": config.EMBEDDING_MODEL,
            "dimensions": config.EMBEDDING_DIMENSIONS,
            "preprocessing_version": config.EMBEDDING_PREPROCESSING_VERSION,
        },
        "chunking": {
            "size": config.CHUNK_SIZE,
            "overlap": config.CHUNK_OVERLAP,
        },
        "analytical_context": config.ANALYTICAL_CONTEXT_ENABLED,
        "document_sections_supported": _document_sections_supported(),
        "contextual_retrieval": {
            "enabled": config.CONTEXTUAL_RETRIEVAL_ENABLED,
            "model": config.CONTEXTUAL_RETRIEVAL_MODEL,
            "max_doc_chars": config.CONTEXTUAL_RETRIEVAL_MAX_DOC_CHARS,
            "batch_size": config.CONTEXTUAL_RETRIEVAL_BATCH_SIZE,
        },
        "sections": [
            {
                "section_index": section.section_index,
                "title": section.title,
                "heading_path": section.heading_path,
                "content": section.content,
                "module": section.module,
                "answer_mode": section.answer_mode,
                "entities": section.entities,
                "semantic_context": section.semantic_context,
                "retrieval_text": _build_section_retrieval_text(title, section),
            }
            for section in sections
        ],
        "chunks": [
            {
                "chunk_index": chunk_index,
                "storage_content": storage_content,
                "retrieval_content": retrieval_content,
                "section_index": section.section_index,
            }
            for chunk_index, storage_content, retrieval_content, section in chunk_items
        ],
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _path_to_document_filename(path: Path, docs_root: Path | None = None) -> str:
    resolved_path = path.resolve()
    if docs_root is not None:
        try:
            return resolved_path.relative_to(docs_root.resolve()).as_posix()
        except ValueError:
            pass
    return path.name


def _get_splitter() -> MarkdownTextSplitter:
    """Retorna instancia reutilizavel do MarkdownTextSplitter."""
    global _splitter
    if _splitter is None:
        _splitter = MarkdownTextSplitter(
            chunk_size=config.CHUNK_SIZE,
            chunk_overlap=config.CHUNK_OVERLAP,
        )
    return _splitter


def chunk_text(text: str) -> list[str]:
    """
    Usa MarkdownTextSplitter para respeitar a hierarquia do documento.
    """
    chunks = _get_splitter().split_text(text)

    if chunks:
        logger.info("Splitting gerou %d chunks. Exemplo do 1o chunk:\n%s...", len(chunks), chunks[0][:100])

    return chunks


# --- P1.2: Headers contextuais nos chunks ---

def _extract_heading_hierarchy(text: str) -> list[tuple[int, str, int]]:
    """Extrai headings Markdown com suas posicoes no texto.
    Retorna: [(nivel, texto_heading, posicao_char), ...]
    """
    headings = []
    for match in re.finditer(r'^(#{1,4})\s+(.+)$', text, re.MULTILINE):
        level = len(match.group(1))
        heading_text = match.group(2).strip()
        headings.append((level, heading_text, match.start()))
    return headings


def _get_heading_context_for_position(
    headings: list[tuple[int, str, int]],
    position: int,
) -> str:
    """Retorna a hierarquia de headings ativa em uma posicao do texto."""
    active: dict[int, str] = {}
    for level, heading_text, hpos in headings:
        if hpos > position:
            break
        active[level] = heading_text
        # Limpar headings mais profundos quando um mais alto aparece
        for deeper in list(active):
            if deeper > level:
                del active[deeper]

    if not active:
        return ""
    parts = [active[level] for level in sorted(active)]
    return " > ".join(parts)


def chunk_text_with_context(text: str, doc_title: str = "") -> list[str]:
    """Chunka texto e prepende contexto de headings a cada chunk."""
    raw_chunks = _get_splitter().split_text(text)
    headings = _extract_heading_hierarchy(text)

    if not headings or not raw_chunks:
        if raw_chunks:
            logger.info("Splitting gerou %d chunks (sem headings).", len(raw_chunks))
        return raw_chunks

    contextualized = []
    search_offset = 0
    for chunk in raw_chunks:
        # Encontrar posicao do chunk no texto original
        snippet = chunk[:80]
        pos = text.find(snippet, search_offset)
        if pos == -1:
            pos = search_offset
        else:
            search_offset = pos + len(chunk) // 2

        heading_ctx = _get_heading_context_for_position(headings, pos)

        prefix_parts = []
        if doc_title:
            prefix_parts.append(f"[{doc_title}]")
        if heading_ctx:
            prefix_parts.append(f"[{heading_ctx}]")

        if prefix_parts:
            prefix = " ".join(prefix_parts) + "\n\n"
            contextualized.append(prefix + chunk)
        else:
            contextualized.append(chunk)

    logger.info(
        "Splitting com contexto gerou %d chunks. Exemplo do 1o chunk:\n%s...",
        len(contextualized),
        contextualized[0][:150],
    )
    return contextualized


# --- Contexto analitico por secao ---

_TABLE_RE = re.compile(r"\b(?:MXS[A-Z0-9_]+|ERP_[A-Z0-9_]+|PC[A-Z0-9_]+|PCT[A-Z0-9_]+|SYNC_[A-Z0-9_]+)\b")
_ENDPOINT_RE = re.compile(r"(?<!\S)(?:/api|api/v\d+)[A-Za-z0-9_./?=&%-]*", flags=re.IGNORECASE)
_CODE_TOKEN_RE = re.compile(r"`([^`]{3,80})`")
_UPPER_TOKEN_RE = re.compile(r"\b[A-Z][A-Z0-9_]{3,}\b")
_STATUS_RE = re.compile(r"\b(?:status|erro|codigo|c[oó]digo)\s*(?:=|:)?\s*[0-9]{1,4}\b", flags=re.IGNORECASE)
_ERROR_LINE_RE = re.compile(r"\b(?:erro|falha|critica|cr[ií]tica|bloqueio|bloqueado|nao|n[aã]o)\b", flags=re.IGNORECASE)


def _unique_limited(values: list[str], limit: int = 30) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = re.sub(r"\s+", " ", str(value or "").strip().strip("`*.,;:()[]{}"))
        if not clean:
            continue
        key = normalize_text(clean)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(clean)
        if len(result) >= limit:
            break
    return result


def _extract_analytical_entities(text: str) -> dict[str, list[str]]:
    tables = _unique_limited([m.group(0).upper() for m in _TABLE_RE.finditer(text)])
    table_keys = {normalize_text(t) for t in tables}

    parameters: list[str] = []
    for match in _CODE_TOKEN_RE.finditer(text):
        token = match.group(1).strip()
        if _UPPER_TOKEN_RE.fullmatch(token) and normalize_text(token) not in table_keys:
            parameters.append(token.upper())

    for line in text.splitlines():
        normalized_line = normalize_text(line)
        if "parametro" not in normalized_line and "configuracao" not in normalized_line:
            continue
        parameters.extend(
            token.upper()
            for token in _UPPER_TOKEN_RE.findall(line)
            if normalize_text(token) not in table_keys
        )

    endpoints = _unique_limited([m.group(0) for m in _ENDPOINT_RE.finditer(text)])
    statuses = _unique_limited([m.group(0) for m in _STATUS_RE.finditer(text)], limit=20)
    error_terms = _unique_limited(
        [line.strip("- *") for line in text.splitlines() if _ERROR_LINE_RE.search(line)],
        limit=12,
    )

    return {
        "tables": tables,
        "parameters": _unique_limited(parameters),
        "endpoints": endpoints,
        "statuses": statuses,
        "error_terms": error_terms,
    }


def _infer_answer_mode(text: str, heading_path: str = "") -> str:
    normalized = normalize_text(f"{heading_path}\n{text}")
    if any(term in normalized for term in ("erro", "falha", "critica", "problema", "nao aparece", "nao sincroniza")):
        return "troubleshooting"
    if any(term in normalized for term in ("select ", " from ", " join ", " tabela", " campo", " coluna", " banco")):
        return "sql_lookup"
    if any(term in normalized for term in ("parametro", "configuracao", "habilitar", "desabilitar", "permissao")):
        return "configuration"
    if any(term in normalized for term in ("integracao", "endpoint", " api", " erp", "json", "extrator")):
        return "integration"
    if any(term in normalized for term in ("pedido", "venda", "orcamento", "rota", "visita", "processo", "fluxo")):
        return "process"
    return "general"


def _infer_section_module(base_module: str, heading_path: str, content: str) -> str:
    inferred = _infer_module("", heading_path, content[:800], "md")
    if inferred != "geral":
        return inferred
    return base_module or "geral"


def _build_semantic_context(
    *,
    doc_title: str,
    heading_path: str,
    module: str,
    answer_mode: str,
    entities: dict[str, list[str]],
) -> str:
    if not config.ANALYTICAL_CONTEXT_ENABLED:
        return ""

    parts = [f"Documento: {doc_title}" if doc_title else ""]
    if heading_path:
        parts.append(f"Secao: {heading_path}")
    if module and module != "geral":
        parts.append(f"Modulo: {module}")
    if answer_mode and answer_mode != "general":
        parts.append(f"Modo de resposta: {answer_mode}")

    entity_parts = []
    for label, key in (
        ("Tabelas", "tables"),
        ("Parametros", "parameters"),
        ("Endpoints", "endpoints"),
        ("Status/codigos", "statuses"),
    ):
        values = entities.get(key) or []
        if values:
            entity_parts.append(f"{label}: {', '.join(values[:8])}")
    if entity_parts:
        parts.append("; ".join(entity_parts))

    return " | ".join(part for part in parts if part).strip()


def _split_markdown_sections(
    text: str,
    *,
    doc_title: str,
    base_module: str,
    doc_type: str,
) -> list[AnalyticalSection]:
    if doc_type.lower() != "md":
        entities = _extract_analytical_entities(text)
        answer_mode = _infer_answer_mode(text, doc_title)
        return [
            AnalyticalSection(
                section_index=0,
                title=doc_title,
                heading_path=doc_title,
                content=text,
                module=base_module,
                answer_mode=answer_mode,
                entities=entities,
                semantic_context=_build_semantic_context(
                    doc_title=doc_title,
                    heading_path=doc_title,
                    module=base_module,
                    answer_mode=answer_mode,
                    entities=entities,
                ),
            )
        ]

    matches = list(re.finditer(r"^(#{1,4})\s+(.+)$", text, re.MULTILINE))
    if not matches:
        entities = _extract_analytical_entities(text)
        answer_mode = _infer_answer_mode(text, doc_title)
        return [
            AnalyticalSection(
                section_index=0,
                title=doc_title,
                heading_path=doc_title,
                content=text,
                module=base_module,
                answer_mode=answer_mode,
                entities=entities,
                semantic_context=_build_semantic_context(
                    doc_title=doc_title,
                    heading_path=doc_title,
                    module=base_module,
                    answer_mode=answer_mode,
                    entities=entities,
                ),
            )
        ]

    sections: list[AnalyticalSection] = []
    active_headings: dict[int, str] = {}

    preamble = text[: matches[0].start()].strip()
    if preamble:
        entities = _extract_analytical_entities(preamble)
        answer_mode = _infer_answer_mode(preamble, doc_title)
        sections.append(
            AnalyticalSection(
                section_index=len(sections),
                title=doc_title,
                heading_path=doc_title,
                content=preamble,
                module=base_module,
                answer_mode=answer_mode,
                entities=entities,
                semantic_context=_build_semantic_context(
                    doc_title=doc_title,
                    heading_path=doc_title,
                    module=base_module,
                    answer_mode=answer_mode,
                    entities=entities,
                ),
            )
        )

    for idx, match in enumerate(matches):
        level = len(match.group(1))
        heading = match.group(2).strip().strip("#").strip()
        active_headings[level] = heading
        for deeper in list(active_headings):
            if deeper > level:
                del active_headings[deeper]

        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        if not content:
            continue

        heading_path = " > ".join(active_headings[lvl] for lvl in sorted(active_headings))
        entities = _extract_analytical_entities(content)
        answer_mode = _infer_answer_mode(content, heading_path)
        section_module = _infer_section_module(base_module, heading_path, content)
        sections.append(
            AnalyticalSection(
                section_index=len(sections),
                title=heading,
                heading_path=heading_path,
                content=content,
                module=section_module,
                answer_mode=answer_mode,
                entities=entities,
                semantic_context=_build_semantic_context(
                    doc_title=doc_title,
                    heading_path=heading_path,
                    module=section_module,
                    answer_mode=answer_mode,
                    entities=entities,
                ),
            )
        )

    return sections


# --- P1.3: Inferencia de prioridade de documento ---

_PRIORITY_HIGH_PREFIXES = ("00-", "01-", "02-", "03-", "04-", "05-")
_PRIORITY_MEDIUM_NAMES = {
    "service-desk-processos.md",
    "service_desk_maxima_organizado.md",
    "glossario.md",
    "base-maxgestao.md",
    "artigos_base.md",
}


def _infer_priority(filename: str, chunk_count: int = 0) -> int:
    """Infere prioridade do documento: 10=core, 8=estruturado, 5=normal, 3=gigante."""
    lower = filename.lower()
    # Core MDs (00-05)
    if any(lower.startswith(p) for p in _PRIORITY_HIGH_PREFIXES) and lower.endswith(".md"):
        return 10
    # Documentos estruturados conhecidos
    if lower in _PRIORITY_MEDIUM_NAMES:
        return 8
    # Documentos gigantes que dominam resultados
    if chunk_count > 400:
        return 3
    return 5

# --- FIM DAS ALTERAÇÕES CRÍTICAS (O RESTO É MANTIDO PARA COMPATIBILIDADE) ---

def _is_quota_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "resource_exhausted" in message or "quota" in message or "429" in message


def _wait_for_embed_slot() -> None:
    global _last_embed_call_at
    min_interval = max(0.0, config.EMBEDDING_MIN_INTERVAL_SECONDS)
    if min_interval <= 0:
        return

    now = time.monotonic()
    wait_for = (_last_embed_call_at + min_interval) - now
    if wait_for > 0:
        time.sleep(wait_for)
    _last_embed_call_at = time.monotonic()


def _retry_delay_seconds(exc: Exception, attempt: int) -> float:
    message = str(exc)
    patterns = [
        r"retry in ([0-9]+(?:\.[0-9]+)?)s",
        r"retryDelay['\"]?:\s*['\"]([0-9]+)s['\"]",
    ]

    for pattern in patterns:
        match = re.search(pattern, message, flags=re.IGNORECASE)
        if match:
            base = float(match.group(1)) + 1.0
            jitter = random.uniform(0.0, max(0.0, config.EMBEDDING_RETRY_JITTER_SECONDS))
            return min(base + jitter, config.EMBEDDING_RETRY_MAX_SECONDS)

    backoff = config.EMBEDDING_RETRY_BASE_SECONDS * (2 ** (attempt - 1))
    jitter = random.uniform(0.0, max(0.0, config.EMBEDDING_RETRY_JITTER_SECONDS))
    return min(backoff + jitter, config.EMBEDDING_RETRY_MAX_SECONDS)


def _embed_batch_with_retry(contents: list[str], filename: str, first_chunk_index: int) -> list[list[float]]:
    for attempt in range(1, config.EMBEDDING_MAX_RETRIES + 1):
        try:
            _wait_for_embed_slot()
            return create_document_embeddings(contents)
        except Exception as exc:
            if not _is_quota_error(exc) or attempt == config.EMBEDDING_MAX_RETRIES:
                raise

            delay = _retry_delay_seconds(exc, attempt)
            logger.warning(
                "Limite de embeddings em %s (chunk %s, tentativa %s/%s). Aguardando %.1fs...",
                filename,
                first_chunk_index,
                attempt,
                config.EMBEDDING_MAX_RETRIES,
                delay,
            )
            time.sleep(delay)

    raise RuntimeError("Falha inesperada ao gerar embeddings")


def _load_failed_report(report_path: Path) -> dict:
    if not report_path.exists():
        return {"updated_at": None, "files": {}}

    try:
        with report_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("files"), dict):
                return data
    except Exception as e:
        logger.warning("Erro ao carregar relatorio de falhas (%s): %s", report_path, e)

    return {"updated_at": None, "files": {}}


def _save_failed_report_entry(
    filename: str,
    source: str,
    total_chunks: int,
    failed_chunks: list[int],
    source_type: str = "file",
) -> None:
    report_path = Path(config.FAILED_INGEST_REPORT)
    report = _load_failed_report(report_path)
    files = report["files"]

    if failed_chunks:
        files[filename] = {
            "source": source,
            "source_type": source_type,
            "total_chunks": total_chunks,
            "failed_chunks": sorted(set(failed_chunks)),
        }
    else:
        files.pop(filename, None)

    report["updated_at"] = datetime.now(timezone.utc).isoformat()
    with report_path.open("w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)


def _failed_sources_from_report() -> tuple[list[Path], list[str]]:
    report = _load_failed_report(Path(config.FAILED_INGEST_REPORT))
    file_sources: list[Path] = []
    url_sources: list[str] = []
    for entry in report.get("files", {}).values():
        source = entry.get("source")
        source_type = (entry.get("source_type") or "").lower()
        if not source:
            continue
        if source_type == "url" or _is_url(source):
            if _is_url(source):
                url_sources.append(source)
            continue
        source_path = Path(source)
        if source_path.exists() and source_path.suffix.lower() in READERS:
            file_sources.append(source_path)
    return file_sources, list(dict.fromkeys(url_sources))


def _is_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except Exception:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _normalized_url_hostname(url: str) -> tuple[str, int]:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except (TypeError, ValueError):
        raise WebFetchError("URL invalida para coleta web.") from None

    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise WebFetchError("URL invalida para coleta web.")
    if parsed.username is not None or parsed.password is not None:
        raise WebFetchError("URL com credenciais embutidas nao e permitida.")

    hostname = parsed.hostname
    if not hostname:
        raise WebFetchError("URL sem host valido.")
    try:
        normalized_host = hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError:
        raise WebFetchError("URL sem host valido.") from None
    if not normalized_host:
        raise WebFetchError("URL sem host valido.")

    return normalized_host, port or (443 if parsed.scheme.lower() == "https" else 80)


def _host_is_allowed(hostname: str) -> bool:
    allowed_hosts = config.WEB_ALLOWED_HOSTS
    if not allowed_hosts:
        return True
    for allowed in allowed_hosts:
        if allowed.startswith("*."):
            suffix = allowed[2:]
            if suffix and hostname.endswith(f".{suffix}"):
                return True
        elif hostname == allowed:
            return True
    return False


def _validate_web_destination(url: str) -> None:
    """Valida politica e todos os enderecos IPv4/IPv6 antes de cada request."""
    hostname, port = _normalized_url_hostname(url)
    if not _host_is_allowed(hostname):
        raise WebFetchError("Destino nao autorizado pela allowlist de coleta web.")

    try:
        resolved = socket.getaddrinfo(
            hostname,
            port,
            family=socket.AF_UNSPEC,
            type=socket.SOCK_STREAM,
        )
    except OSError:
        raise WebFetchError("Nao foi possivel resolver o destino da coleta web.") from None

    addresses = {entry[4][0].split("%", 1)[0] for entry in resolved if entry[4]}
    if not addresses:
        raise WebFetchError("Nao foi possivel resolver o destino da coleta web.")
    for raw_address in addresses:
        try:
            address = ipaddress.ip_address(raw_address)
        except ValueError:
            raise WebFetchError("A resolucao retornou um endereco invalido.") from None
        comparable_address = (
            address.ipv4_mapped or address
            if isinstance(address, ipaddress.IPv6Address)
            else address
        )
        if (
            not comparable_address.is_global
            or comparable_address.is_private
            or comparable_address.is_loopback
            or comparable_address.is_link_local
            or comparable_address.is_multicast
            or comparable_address.is_reserved
            or comparable_address.is_unspecified
        ):
            raise WebFetchError("Destino de rede bloqueado pela politica de coleta web.")


def _url_source_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8", errors="ignore")).hexdigest()[:12]



def _to_slug(value: str, default: str = "source") -> str:
    value = re.sub(r"[^a-zA-Z0-9._-]+", "_", value).strip("_")
    return value or default


def _url_to_filename(url: str) -> str:
    parsed = urlparse(url)
    host = _to_slug(parsed.netloc.replace(":", "_"), default="web")
    path = parsed.path.strip("/")
    base = _to_slug(path.split("/")[-1] if path else "index", default="index")
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:10]
    return f"url__{host}__{base}__{digest}"


def _title_from_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.path and parsed.path != "/":
        leaf = parsed.path.rstrip("/").split("/")[-1]
        return leaf or parsed.netloc
    return parsed.netloc


def _title_to_filename(title: str) -> str:
    normalized = unicodedata.normalize("NFKD", title or "")
    without_accents = "".join(ch for ch in normalized if not unicodedata.combining(ch))
    upper = without_accents.upper()
    upper = re.sub(r"\bCOM\b", " ", upper)
    upper = re.sub(r"[^A-Z0-9]+", "_", upper)
    upper = re.sub(r"_+", "_", upper).strip("_")
    return upper[:120]



_MODULE_HINTS_BY_FILENAME = [
    ("01 parametros e configuracao", "parametros_configuracao"),
    ("02 pedidos e vendas", "pedidos_vendas"),
    ("03 campanhas e descontos", "campanhas_descontos"),
    ("04 rotas visitas e consultas", "rotas_visitas_consultas"),
    ("05 sql banco e integracao", "sql_integracao"),
    ("layout integracao", "sql_integracao"),
    ("service desk", "suporte_processos"),
    ("glossario", "glossario"),
    ("tipos de venda", "pedidos_vendas"),
    ("maxpag", "financeiro_pagamentos"),
    ("base maxgestao", "gestao_operacional"),
]



def _infer_module(filename: str, title: str, source: str, doc_type: str) -> str:
    joined = normalize_text(f"{filename} {title} {source} {doc_type}")
    padded = f" {joined} "

    for filename_hint, module in _MODULE_HINTS_BY_FILENAME:
        if filename_hint in joined:
            return module

    if doc_type.lower() == "sql":
        return "sql_integracao"

    for module, hints in QUERY_MODULE_HINTS.items():
        if any(f" {hint} " in padded for hint in hints):
            return module

    return "geral"


def _get_http_client() -> httpx.Client:
    global _http_client
    if _http_client is None:
        _http_client = httpx.Client(
            timeout=config.WEB_FETCH_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
            headers={
                "User-Agent": config.WEB_USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
                "Cache-Control": "no-cache",
            },
        )
    return _http_client


def _download_web_content(
    url: str,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[bytes, str, str, str]:
    """Baixa uma fonte com redirects manuais e limite incremental de bytes."""
    current_url = url
    max_redirects = max(0, int(config.WEB_MAX_REDIRECTS))
    max_bytes = max(1, int(config.WEB_MAX_DOWNLOAD_BYTES))
    timeout_seconds = max(0.1, float(config.WEB_FETCH_TIMEOUT_SECONDS))
    deadline = time.monotonic() + timeout_seconds

    for redirect_count in range(max_redirects + 1):
        _validate_web_destination(current_url)
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            raise WebFetchError("Tempo limite da coleta web excedido.")
        try:
            with _get_http_client().stream(
                "GET",
                current_url,
                headers=headers,
                timeout=remaining_seconds,
            ) as response:
                status_code = response.status_code
                if 300 <= status_code < 400:
                    location = response.headers.get("Location")
                    if status_code not in _REDIRECT_STATUS_CODES or not location:
                        raise WebFetchError("Redirecionamento HTTP invalido.")
                    if redirect_count >= max_redirects:
                        raise WebFetchError("Limite de redirecionamentos excedido.")
                    current_url = urljoin(str(response.url), location)
                    continue

                if status_code >= 400:
                    raise WebFetchError(
                        f"Falha HTTP na coleta web (status {status_code}).",
                        status_code=status_code,
                    )

                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        declared_bytes = int(content_length)
                    except ValueError:
                        declared_bytes = None
                    if declared_bytes is not None and declared_bytes > max_bytes:
                        raise WebFetchError("Resposta excede o limite de download.")

                chunks: list[bytes] = []
                downloaded_bytes = 0
                chunk_size = min(64 * 1024, max_bytes + 1)
                for chunk in response.iter_bytes(chunk_size=chunk_size):
                    if time.monotonic() >= deadline:
                        raise WebFetchError("Tempo limite da coleta web excedido.")
                    downloaded_bytes += len(chunk)
                    if downloaded_bytes > max_bytes:
                        raise WebFetchError("Resposta excede o limite de download.")
                    chunks.append(chunk)

                content_type = response.headers.get("Content-Type") or ""
                encoding = response.encoding or "utf-8"
                return b"".join(chunks), content_type, current_url, encoding
        except WebFetchError:
            raise
        except httpx.HTTPError as exc:
            raise WebFetchError(
                f"Falha de rede na coleta web ({type(exc).__name__})."
            ) from None

    raise WebFetchError("Limite de redirecionamentos excedido.")


def _content_type_to_doc_type(content_type: str) -> str:
    ct = (content_type or "").lower()
    if "pdf" in ct:
        return "pdf"
    if "html" in ct:
        return "html"
    if "json" in ct:
        return "json"
    if "xml" in ct:
        return "xml"
    if "csv" in ct:
        return "csv"
    if "markdown" in ct:
        return "md"
    if "text/" in ct:
        return "txt"
    return "web"


def _clean_page_title(raw_title: str) -> str:
    title = re.sub(r"\s+", " ", (raw_title or "").strip())
    if not title:
        return ""
    for sep in (" - ", " | ", " — ", " – "):
        if sep in title:
            head = title.split(sep, 1)[0].strip()
            if len(head) >= 4:
                title = head
                break
    return title


def _html_to_text(html: str) -> tuple[str, str | None]:
    # Mantido para leitura de sites (HTML)
    page_title: str | None = None
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        og_title = soup.find("meta", attrs={"property": "og:title"})
        if og_title and og_title.get("content"):
            page_title = str(og_title.get("content")).strip()
        if not page_title:
            h1 = soup.find("h1")
            if h1:
                page_title = h1.get_text(" ", strip=True)
        if not page_title and soup.title and soup.title.string:
            page_title = soup.title.string.strip()
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = soup.get_text("\n")
    except Exception:
        text = re.sub(r"<[^>]+>", " ", html)
        page_title = None

    lines = []
    for line in text.splitlines():
        clean = re.sub(r"\s+", " ", line).strip()
        if clean:
            lines.append(clean)
    return "\n".join(lines), _clean_page_title(page_title or "")


def _parse_zendesk_article_reference(url: str) -> tuple[str, str, str]:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4 or parts[0].lower() != "hc" or parts[2].lower() != "articles":
        raise ValueError("URL nao parece ser artigo Zendesk")
    locale = parts[1]
    raw_article = parts[3]
    match = re.match(r"(\d+)", raw_article)
    if not match:
        raise ValueError("Nao foi possivel identificar article_id na URL")
    article_id = match.group(1)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    return base_url, locale, article_id


def _read_zendesk_article_api(url: str) -> tuple[str, str, str | None] | None:
    try:
        base_url, locale, article_id = _parse_zendesk_article_reference(url)
    except ValueError:
        return None
    api_urls = [
        f"{base_url}/api/v2/help_center/{locale}/articles/{article_id}.json",
        f"{base_url}/api/v2/help_center/articles/{article_id}.json",
    ]
    for api_url in api_urls:
        try:
            data, _content_type, _final_url, encoding = _download_web_content(
                api_url,
                headers={"Accept": "application/json"},
            )
            payload = json.loads(data.decode(encoding, errors="replace"))
        except (WebFetchError, UnicodeError, json.JSONDecodeError):
            continue
        article = payload.get("article") if isinstance(payload, dict) else None
        if not isinstance(article, dict):
            continue
        title = _clean_page_title(str(article.get("title") or ""))
        body = str(article.get("body") or "")
        text, title_from_body = _html_to_text(body)
        if not title:
            title = title_from_body or _title_from_url(url)
        if text.strip():
            logger.info(
                "Fonte web source_id=%s lida via API Zendesk.",
                _url_source_id(url),
            )
            return text, "html", title
    return None


def read_url(url: str) -> tuple[str, str, str | None]:
    if not _is_url(url):
        raise WebFetchError("URL invalida para coleta web.")
    try:
        data, content_type_header, final_url, encoding = _download_web_content(url)
    except WebFetchError as exc:
        if exc.status_code in {401, 403}:
            fallback = _read_zendesk_article_api(url)
            if fallback is not None:
                return fallback
        raise
    content_type = content_type_header.split(";")[0].strip().lower()
    lower_url = final_url.lower()
    page_title: str | None = None

    try:
        # Detecção se é PDF (URL ou Content-Type)
        if content_type == "application/pdf" or lower_url.endswith(".pdf"):
            text = read_pdf_bytes(data)
            doc_type = "pdf"
        else:
            doc_type = _content_type_to_doc_type(content_type)
            decoded_text = data.decode(encoding, errors="replace")
            if doc_type == "html":
                text, page_title = _html_to_text(decoded_text)
            else:
                text = decoded_text
    except Exception as exc:
        raise WebFetchError(
            f"Nao foi possivel processar a fonte web ({type(exc).__name__})."
        ) from None

    if not text.strip():
        raise WebFetchError("A fonte web retornou conteudo vazio.")

    max_chars = max(1000, config.WEB_MAX_TEXT_CHARS)
    if len(text) > max_chars:
        logger.warning(
            "Conteudo web source_id=%s truncado para %s caracteres.",
            _url_source_id(url),
            max_chars,
        )
        text = text[:max_chars]

    return text, doc_type, page_title


def _load_urls_from_file(filepath: str) -> list[str]:
    path = Path(filepath)
    if not path.exists():
        logger.warning("Arquivo de URLs nao encontrado: %s", path)
        return []
    urls: list[str] = []
    with path.open("r", encoding="utf-8", errors="ignore") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            candidates = re.findall(r"https?://[^\s)\]>\"']+", line)
            if not candidates and _is_url(line):
                candidates = [line]
            for url in candidates:
                if _is_url(url):
                    urls.append(url)
    return list(dict.fromkeys(urls))




# --- Contextual Retrieval (tecnica Anthropic) ---

_last_contextual_call_at = 0.0
_CONTEXTUAL_MIN_INTERVAL = 2.0  # segundos entre chamadas (evita 429)
_CONTEXTUAL_BATCH_SIZE = max(1, int(config.CONTEXTUAL_RETRIEVAL_BATCH_SIZE))  # chunks por chamada LLM


def _extract_json_fragment(raw_text: str) -> str:
    text = (raw_text or "").strip()
    if not text:
        return text

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    for opening, closing in (("[", "]"), ("{", "}")):
        start = text.find(opening)
        end = text.rfind(closing)
        if start >= 0 and end > start:
            candidate = text[start:end + 1].strip()
            if candidate:
                return candidate
    return text


def _context_to_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("context", "text", "texto", "summary", "value"):
            field = value.get(key)
            if isinstance(field, str) and field.strip():
                return field.strip()
    return str(value).strip()


def _normalize_contextual_payload(payload, expected_count: int) -> dict[int, str]:
    contexts: dict[int, str] = {}

    if isinstance(payload, dict):
        if isinstance(payload.get("contexts"), list):
            payload = payload["contexts"]
        else:
            for key, value in payload.items():
                try:
                    idx = int(key)
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < expected_count:
                    text = _context_to_text(value)
                    if text:
                        contexts[idx] = text
            return contexts

    if not isinstance(payload, list):
        return contexts

    # Formato 1: lista simples de strings (ordem do chunk)
    if payload and all(not isinstance(item, dict) for item in payload):
        for idx, value in enumerate(payload):
            if idx >= expected_count:
                break
            text = _context_to_text(value)
            if text:
                contexts[idx] = text
        return contexts

    # Formato 2: lista de objetos com id/context
    for idx, item in enumerate(payload):
        if isinstance(item, dict):
            raw_id = item.get("id", item.get("chunk_id", idx))
            try:
                context_idx = int(raw_id)
            except (TypeError, ValueError):
                context_idx = idx
            if not (0 <= context_idx < expected_count):
                continue
            text = _context_to_text(item)
            if text:
                contexts[context_idx] = text
            continue

        if idx < expected_count:
            text = _context_to_text(item)
            if text:
                contexts[idx] = text

    return contexts


def _wait_for_contextual_slot() -> None:
    """Rate limiting para chamadas ao Haiku — evita 429 Too Many Requests."""
    global _last_contextual_call_at
    now = time.monotonic()
    wait_for = (_last_contextual_call_at + _CONTEXTUAL_MIN_INTERVAL) - now
    if wait_for > 0:
        time.sleep(wait_for)
    _last_contextual_call_at = time.monotonic()


def _contextualize_chunks_batch(
    *,
    chunks_with_indices: list[tuple[int, str]],
    full_document: str,
    filename: str,
) -> list[tuple[int, str]]:
    """
    Contextual Retrieval em batch (Anthropic): envia varios chunks numa unica
    chamada ao LLM e recebe contextos para todos de uma vez.

    Reduz drasticamente o numero de chamadas API (ex: 120 chunks / 10 por batch = 12 chamadas).
    Em caso de erro, retorna os chunks originais (zero degradacao).

    Ref: https://www.anthropic.com/news/contextual-retrieval
    """
    if not config.CONTEXTUAL_RETRIEVAL_ENABLED or not chunks_with_indices:
        return chunks_with_indices

    truncated_doc = full_document[:config.CONTEXTUAL_RETRIEVAL_MAX_DOC_CHARS]

    # Montar a lista de chunks numerados no prompt
    chunks_section = ""
    for idx, (chunk_index, content) in enumerate(chunks_with_indices):
        chunks_section += f"<chunk id=\"{idx}\">\n{content}\n</chunk>\n\n"

    system_prompt = (
        "Voce e um assistente que situa trechos de documentos tecnicos no contexto geral "
        "do sistema maxPedido (Maxima Sistemas). "
        "Responda APENAS com um JSON array valido, sem explicacoes adicionais."
    )

    user_prompt = (
        f"<documento>\n{truncated_doc}\n</documento>\n\n"
        f"Abaixo estao {len(chunks_with_indices)} chunks extraidos deste documento:\n\n"
        f"{chunks_section}"
        "Para CADA chunk, forneca um contexto MUITO CURTO (maximo 20 palavras, 1 frase) que situe "
        "o chunk no documento geral, melhorando a busca por similaridade semantica. "
        "Inclua: secao, tema principal e termos tecnicos chave (tabelas, parametros, modulos). "
        "IMPORTANTE: seja extremamente conciso — 1 frase curta por chunk.\n\n"
        "Responda com um JSON array onde cada elemento e o contexto do chunk correspondente, "
        "na mesma ordem. Exemplo para 3 chunks:\n"
        '[\"contexto do chunk 0\", \"contexto do chunk 1\", \"contexto do chunk 2\"]\n\n'
        "JSON array:"
    )

    max_tokens = 65536  # max output do Gemini 2.5 Flash

    try:
        from rag import _gemini_generate

        _wait_for_contextual_slot()
        response = _gemini_generate(
            model=config.CONTEXTUAL_RETRIEVAL_MODEL,
            max_tokens=max_tokens,
            system=system_prompt,
            contents=user_prompt,
        )
        raw_text = response.text.strip()

        payload = json.loads(_extract_json_fragment(raw_text))
        contexts_map = _normalize_contextual_payload(payload, len(chunks_with_indices))

        if not contexts_map:
            logger.warning(
                "Contextual Retrieval batch para %s: resposta sem contextos aproveitaveis. Usando chunks originais.",
                filename,
            )
            return chunks_with_indices

        # Aplicar contextos aos chunks (fallback parcial para os faltantes)
        result = []
        for local_idx, (chunk_index, content) in enumerate(chunks_with_indices):
            ctx = contexts_map.get(local_idx, "")
            if ctx:
                result.append((chunk_index, f"{ctx}\n\n{content}"))
            else:
                result.append((chunk_index, content))

        contextualized_count = sum(1 for local_idx in range(len(chunks_with_indices)) if contexts_map.get(local_idx))
        if contextualized_count < len(chunks_with_indices):
            logger.warning(
                "Contextual Retrieval batch para %s: esperado=%d, recebido=%d, aplicado=%d (fallback parcial).",
                filename,
                len(chunks_with_indices),
                len(contexts_map),
                contextualized_count,
            )

        logger.info(
            "Contextual Retrieval: %d/%d chunks contextualizados para %s",
            contextualized_count,
            len(chunks_with_indices),
            filename,
        )
        return result

    except json.JSONDecodeError as e:
        logger.warning(
            "Contextual Retrieval batch para %s: JSON invalido: %s. Raw: %.200r. Usando chunks originais.",
            filename, e, raw_text,
        )
        return chunks_with_indices

    except Exception as e:
        logger.warning(
            "Contextual Retrieval batch para %s falhou: %s. Usando chunks originais.",
            filename, e,
        )
        return chunks_with_indices


def _build_chunk_row(
    *,
    doc_id: str,
    chunk_index: int,
    clean_content: str,
    filename: str,
    doc_type: str,
    source_type: str,
    module: str,
    title: str,
    doc_priority: int,
    content_hash: str,
    processing_hash: str,
    embedding: list[float],
    section: AnalyticalSection | None = None,
) -> dict:
    metadata = {
        "filename": filename,
        "chunk_index": chunk_index,
        "doc_type": doc_type,
        "source_type": source_type,
        "module": section.module if section else module,
        "title": title,
        "doc_priority": doc_priority,
        "content_hash": content_hash,
        "processing_hash": processing_hash,
    }
    if section:
        metadata.update(
            {
                "section_index": section.section_index,
                "section_title": section.title,
                "heading_path": section.heading_path,
                "semantic_context": section.semantic_context,
                "entities": section.entities,
                "answer_mode": section.answer_mode,
            }
        )

    row = {
        "document_id": doc_id,
        "content": clean_content,
        "chunk_index": chunk_index,
        "metadata": metadata,
        "embedding": embedding_to_pgvector(embedding),
        "token_count": len(clean_content.split()),
        "module": section.module if section else module,
        "doc_type": doc_type,
        "source_type": source_type,
        "doc_priority": doc_priority,
    }
    if section and section.section_id:
        row.update(
            {
                "section_id": section.section_id,
                "heading_path": section.heading_path,
                "semantic_context": section.semantic_context,
                "entities": section.entities,
                "answer_mode": section.answer_mode,
            }
        )
    return row


def _document_sections_supported() -> bool:
    global _document_sections_available
    if _document_sections_available is not None:
        return _document_sections_available

    try:
        supabase_select(
            "document_sections",
            select="id,retrieval_text,embedding",
            filters={"limit": 1},
        )
        _document_sections_available = True
    except Exception as exc:
        _document_sections_available = False
        logger.warning(
            "Camada completa de document_sections indisponivel (%s). "
            "Gravando contexto analitico apenas em metadata JSONB.",
            exc,
        )
    return _document_sections_available


def _prepare_section_retrieval_data(
    title: str,
    sections: list[AnalyticalSection],
) -> list[tuple[AnalyticalSection, str, list[float]]]:
    persisted_sections = [section for section in sections if section.section_id]
    if persisted_sections:
        ensure_embedding_index_identity("sections")
    prepared: list[tuple[AnalyticalSection, str, list[float]]] = []
    batch_size = max(1, config.EMBEDDING_BATCH_SIZE)
    for batch_start in range(0, len(persisted_sections), batch_size):
        section_batch = persisted_sections[batch_start : batch_start + batch_size]
        payloads = [
            _build_section_retrieval_text(title, section)
            for section in section_batch
        ]
        embeddings = _embed_batch_with_retry(
            payloads,
            filename=f"{title}#sections",
            first_chunk_index=batch_start,
        )
        if len(embeddings) != len(payloads):
            raise RuntimeError(
                f"embeddings de secoes incompletos: esperado {len(payloads)}, recebido {len(embeddings)}"
            )
        prepared.extend(zip(section_batch, payloads, embeddings))
    return prepared


def _persist_section_retrieval_data(
    doc_id: str,
    title: str,
    sections: list[AnalyticalSection],
    *,
    connection=None,
) -> None:
    prepared = _prepare_section_retrieval_data(title, sections)
    for section, retrieval_text, embedding in prepared:
        supabase_update(
            "document_sections",
            {
                "retrieval_text": retrieval_text,
                "embedding": embedding_to_pgvector(embedding),
                "metadata": {
                    "source": "ingest.py",
                    "document_id": str(doc_id),
                    "retrieval_ready": True,
                },
            },
            {"id": f"eq.{section.section_id}"},
            connection=connection,
        )


def _prepare_document_section_rows(
    doc_id: str,
    title: str,
    sections: list[AnalyticalSection],
    *,
    content_hash: str,
    processing_hash: str,
) -> list[dict]:
    if not _document_sections_supported():
        return []

    for section in sections:
        section.section_id = str(uuid4())

    prepared = _prepare_section_retrieval_data(title, sections)
    return [
        {
            "id": section.section_id,
            "document_id": doc_id,
            "section_index": section.section_index,
            "heading_path": section.heading_path,
            "title": section.title,
            "module": section.module,
            "answer_mode": section.answer_mode,
            "semantic_context": section.semantic_context,
            "entities": section.entities,
            "retrieval_text": retrieval_text,
            "embedding": embedding_to_pgvector(embedding),
            "metadata": {
                "source": "ingest.py",
                "document_id": str(doc_id),
                "retrieval_ready": True,
                "content_hash": content_hash,
                "processing_hash": processing_hash,
            },
        }
        for section, retrieval_text, embedding in prepared
    ]


def _find_reusable_document(processing_hash: str, filename: str) -> dict | None:
    candidates = supabase_select(
        "documents",
        select="id,filename",
        filters={
            "processing_hash": f"eq.{processing_hash}",
            "order": "created_at.asc",
            "limit": 10,
        },
    )
    return next(
        (row for row in candidates if row.get("filename") != filename),
        None,
    )


def _clone_prepared_rows(
    *,
    source_document_id: str,
    document_id: str,
    filename: str,
    title: str,
    doc_type: str,
    source_type: str,
    module: str,
    doc_priority: int,
    content_hash: str,
    processing_hash: str,
) -> tuple[list[dict], list[dict]]:
    """Reaproveita vetores de um processamento identico sem perder a nova origem."""
    section_rows: list[dict] = []
    section_id_map: dict[str, str] = {}
    if _document_sections_supported():
        source_sections = supabase_select(
            "document_sections",
            select="*",
            filters={
                "document_id": f"eq.{source_document_id}",
                "order": "section_index.asc",
            },
        )
        section_columns = (
            "section_index",
            "heading_path",
            "title",
            "module",
            "answer_mode",
            "semantic_context",
            "entities",
            "retrieval_text",
            "embedding",
        )
        for source_section in source_sections:
            source_section_id = str(source_section["id"])
            cloned_section_id = str(uuid4())
            section_id_map[source_section_id] = cloned_section_id
            metadata = dict(source_section.get("metadata") or {})
            metadata.update(
                {
                    "document_id": document_id,
                    "content_hash": content_hash,
                    "processing_hash": processing_hash,
                }
            )
            section_rows.append(
                {
                    "id": cloned_section_id,
                    "document_id": document_id,
                    **{
                        column: source_section.get(column)
                        for column in section_columns
                    },
                    "metadata": metadata,
                }
            )

    source_chunks = supabase_select(
        "document_chunks",
        select="*",
        filters={
            "document_id": f"eq.{source_document_id}",
            "order": "chunk_index.asc",
        },
    )
    chunk_columns = (
        "content",
        "chunk_index",
        "embedding",
        "token_count",
        "heading_path",
        "semantic_context",
        "entities",
        "answer_mode",
    )
    chunk_rows: list[dict] = []
    for source_chunk in source_chunks:
        metadata = dict(source_chunk.get("metadata") or {})
        metadata.update(
            {
                "filename": filename,
                "doc_type": doc_type,
                "source_type": source_type,
                "module": module,
                "title": title,
                "doc_priority": doc_priority,
                "content_hash": content_hash,
                "processing_hash": processing_hash,
            }
        )
        row = {
            "document_id": document_id,
            **{column: source_chunk.get(column) for column in chunk_columns},
            "metadata": metadata,
            "module": module,
            "doc_type": doc_type,
            "source_type": source_type,
            "doc_priority": doc_priority,
        }
        source_section_id = source_chunk.get("section_id")
        if source_section_id is not None:
            row["section_id"] = section_id_map.get(str(source_section_id))
        chunk_rows.append(row)
    return section_rows, chunk_rows


def _insert_chunk_rows(rows: list[dict], *, connection) -> set[int]:
    if not rows:
        return set()

    inserted_indices: set[int] = set()
    batch_size = max(1, int(config.INGEST_DB_BATCH_SIZE))
    for batch_start in range(0, len(rows), batch_size):
        batch_rows = rows[batch_start : batch_start + batch_size]
        supabase_insert("document_chunks", batch_rows, connection=connection)
        inserted_indices.update(int(row["chunk_index"]) for row in batch_rows)
    return inserted_indices


def _insert_rows_in_batches(table: str, rows: list[dict], *, connection) -> None:
    batch_size = max(1, int(config.INGEST_DB_BATCH_SIZE))
    for batch_start in range(0, len(rows), batch_size):
        supabase_insert(
            table,
            rows[batch_start : batch_start + batch_size],
            connection=connection,
        )


def _replace_document_atomically(
    *,
    filename: str,
    document_row: dict,
    section_rows: list[dict],
    chunk_rows: list[dict],
    force: bool,
) -> bool:
    """Troca uma fonte completa; em reindexes concorrentes, o ultimo commit vence."""
    with db_transaction() as connection:
        db_advisory_xact_lock(f"ingest:{filename}", connection=connection)
        existing = supabase_select(
            "documents",
            select="id,processing_hash",
            filters={"filename": f"eq.{filename}"},
            connection=connection,
        )
        if (
            existing
            and not force
            and existing[0].get("processing_hash") == document_row["processing_hash"]
        ):
            return False

        if existing:
            old_doc_id = existing[0]["id"]
            logger.info("Substituindo documento anterior de forma atomica: %s", filename)
            supabase_delete("documents", "id", old_doc_id, connection=connection)

        supabase_insert("documents", document_row, connection=connection)
        if section_rows:
            _insert_rows_in_batches(
                "document_sections",
                section_rows,
                connection=connection,
            )
        inserted_indices = _insert_chunk_rows(chunk_rows, connection=connection)
        if len(inserted_indices) != len(chunk_rows):
            raise RuntimeError(
                f"troca incompleta: {len(inserted_indices)}/{len(chunk_rows)} chunks inseridos"
            )
    return True


def _prepare_chunk_rows(
    *,
    doc_id: str,
    filename: str,
    title: str,
    text: str,
    doc_type: str,
    source_type: str,
    module: str,
    doc_priority: int,
    chunk_items: list[tuple[int, str, str, AnalyticalSection]],
    content_hash: str = "",
    processing_hash: str = "",
) -> tuple[list[dict], list[int]]:
    if chunk_items:
        ensure_embedding_index_identity("corpus")
    rows: list[dict] = []
    failed_chunks: list[int] = []
    batch_size = max(1, config.EMBEDDING_BATCH_SIZE)
    for batch_start in range(0, len(chunk_items), batch_size):
        batch = chunk_items[batch_start : batch_start + batch_size]
        clean_batch: list[tuple[int, str, str, AnalyticalSection]] = []

        for chunk_index, storage_content, retrieval_content, section in batch:
            clean_storage = storage_content.replace("\x00", "").strip()
            clean_retrieval = retrieval_content.replace("\x00", "").strip()
            if not clean_storage or not clean_retrieval:
                failed_chunks.append(chunk_index)
                continue
            clean_batch.append((chunk_index, clean_storage, clean_retrieval, section))

        for ctx_start in range(0, len(clean_batch), _CONTEXTUAL_BATCH_SIZE):
            ctx_sub = clean_batch[ctx_start : ctx_start + _CONTEXTUAL_BATCH_SIZE]
            ctx_pairs = [
                (chunk_index, retrieval_content)
                for chunk_index, _clean_storage, retrieval_content, _section in ctx_sub
            ]
            ctx_result = _contextualize_chunks_batch(
                chunks_with_indices=ctx_pairs,
                full_document=text,
                filename=filename,
            )
            by_index = {
                chunk_index: (clean_storage, section)
                for chunk_index, clean_storage, _retrieval_content, section in ctx_sub
            }
            clean_batch[ctx_start : ctx_start + _CONTEXTUAL_BATCH_SIZE] = [
                (
                    chunk_index,
                    by_index[chunk_index][0],
                    contextualized_content,
                    by_index[chunk_index][1],
                )
                for chunk_index, contextualized_content in ctx_result
            ]

        if not clean_batch:
            continue

        indices = [item[0] for item in clean_batch]
        storage_contents = [item[1] for item in clean_batch]
        retrieval_contents = [item[2] for item in clean_batch]
        sections_for_batch = [item[3] for item in clean_batch]

        try:
            embeddings = _embed_batch_with_retry(retrieval_contents, filename, indices[0])
            if len(embeddings) != len(clean_batch):
                raise RuntimeError(
                    f"embeddings incompletos: esperado {len(clean_batch)}, recebido {len(embeddings)}"
                )
            batch_rows = [
                _build_chunk_row(
                    doc_id=doc_id,
                    chunk_index=chunk_index,
                    clean_content=storage_content,
                    filename=filename,
                    doc_type=doc_type,
                    source_type=source_type,
                    module=module,
                    title=title,
                    doc_priority=doc_priority,
                    content_hash=content_hash,
                    processing_hash=processing_hash,
                    embedding=embedding,
                    section=section,
                )
                for chunk_index, storage_content, section, embedding in zip(
                    indices,
                    storage_contents,
                    sections_for_batch,
                    embeddings,
                )
            ]
            rows.extend(batch_rows)
        except Exception as batch_error:
            logger.error(
                "Erro no lote de %s (chunk inicial %s): %s. Fallback chunk a chunk...",
                filename,
                batch_start,
                batch_error,
            )
            for chunk_index, storage_content, retrieval_content, section in clean_batch:
                try:
                    embedding = _embed_batch_with_retry(
                        [retrieval_content],
                        filename,
                        chunk_index,
                    )[0]
                    rows.append(
                        _build_chunk_row(
                            doc_id=doc_id,
                            chunk_index=chunk_index,
                            clean_content=storage_content,
                            filename=filename,
                            doc_type=doc_type,
                            source_type=source_type,
                            module=module,
                            title=title,
                            doc_priority=doc_priority,
                            content_hash=content_hash,
                            processing_hash=processing_hash,
                            embedding=embedding,
                            section=section,
                        )
                    )
                except Exception as chunk_error:
                    failed_chunks.append(chunk_index)
                    logger.error("Erro no chunk %s de %s: %s", chunk_index, filename, chunk_error)

    return rows, sorted(set(failed_chunks))


def _ingest_text_source(
    *,
    filename: str,
    title: str,
    source: str,
    doc_type: str,
    text: str,
    source_type: str,
    force: bool,
) -> dict:
    if not text.strip():
        logger.warning("Fonte vazia: %s", filename)
        return {
            "filename": filename,
            "chunks_count": 0,
            "failed_chunks": 0,
            "error": "fonte vazia",
        }

    module = _infer_module(filename, title, source, doc_type)
    sections = _split_markdown_sections(
        text,
        doc_title=title,
        base_module=module,
        doc_type=doc_type,
    )
    chunk_items: list[tuple[int, str, str, AnalyticalSection]] = []
    for section in sections:
        section_chunks = _get_splitter().split_text(section.content)
        if not section_chunks and section.content.strip():
            section_chunks = [section.content.strip()]
        for raw_chunk in section_chunks:
            storage_content = raw_chunk
            retrieval_content = raw_chunk
            if section.semantic_context:
                retrieval_content = f"{section.semantic_context}\n\n{raw_chunk}"
            chunk_items.append((len(chunk_items), storage_content, retrieval_content, section))

    logger.info(
        "%s chunks gerados para %s em %s secoes analiticas",
        len(chunk_items),
        filename,
        len(sections),
    )
    logger.info("Modulo inferido para %s: %s", filename, module)
    if not chunk_items:
        logger.error("Nenhum chunk valido gerado para %s.", filename)
        return {
            "filename": filename,
            "chunks_count": 0,
            "failed_chunks": 0,
            "error": "nenhum chunk gerado",
        }

    doc_priority = _infer_priority(filename, chunk_count=len(chunk_items))
    content_digest = _content_hash(text)
    processing_digest = _processing_hash(
        content_hash=content_digest,
        title=title,
        doc_type=doc_type,
        module=module,
        doc_priority=doc_priority,
        sections=sections,
        chunk_items=chunk_items,
    )
    existing = supabase_select(
        "documents",
        select="id,processing_hash",
        filters={"filename": f"eq.{filename}"},
    )
    if (
        existing
        and not force
        and existing[0].get("processing_hash") == processing_digest
    ):
        supabase_update(
            "documents",
            {
                "title": title,
                "source": source,
                "doc_type": doc_type,
                "content_hash": content_digest,
                "priority": doc_priority,
            },
            {"id": f"eq.{existing[0]['id']}"},
        )
        _save_failed_report_entry(
            filename=filename,
            source=source,
            total_chunks=len(chunk_items),
            failed_chunks=[],
            source_type=source_type,
        )
        logger.info("Pulando %s: conteudo e preprocessamento nao mudaram.", filename)
        return {
            "filename": filename,
            "chunks_count": 0,
            "failed_chunks": 0,
            "skipped": True,
            "unchanged": True,
            "content_hash": content_digest,
            "processing_hash": processing_digest,
        }

    doc_id = str(uuid4())
    all_chunk_indices = [item[0] for item in chunk_items]
    section_rows: list[dict] = []
    chunk_rows: list[dict] = []
    failed_chunks: list[int] = []
    reused_document = None if force else _find_reusable_document(processing_digest, filename)

    try:
        if reused_document:
            section_rows, chunk_rows = _clone_prepared_rows(
                source_document_id=str(reused_document["id"]),
                document_id=doc_id,
                filename=filename,
                title=title,
                doc_type=doc_type,
                source_type=source_type,
                module=module,
                doc_priority=doc_priority,
                content_hash=content_digest,
                processing_hash=processing_digest,
            )
            logger.info(
                "Reaproveitando embeddings de %s para a origem %s.",
                reused_document.get("filename"),
                filename,
            )
        else:
            if config.CONTEXTUAL_RETRIEVAL_ENABLED:
                model_cfg = get_model_config()
                logger.info(
                    "Contextual Retrieval ATIVO para %s (%d chunks serao enriquecidos via %s/%s)",
                    filename,
                    len(chunk_items),
                    model_cfg.get("llm_provider", "gemini"),
                    model_cfg.get("contextual_model", config.CONTEXTUAL_RETRIEVAL_MODEL),
                )
            section_rows = _prepare_document_section_rows(
                doc_id,
                title,
                sections,
                content_hash=content_digest,
                processing_hash=processing_digest,
            )
            chunk_rows, failed_chunks = _prepare_chunk_rows(
                doc_id=doc_id,
                filename=filename,
                title=title,
                text=text,
                doc_type=doc_type,
                source_type=source_type,
                module=module,
                doc_priority=doc_priority,
                content_hash=content_digest,
                processing_hash=processing_digest,
                chunk_items=chunk_items,
            )
    except Exception as preparation_error:
        logger.error(
            "Falha ao preparar reingestao de %s; versao anterior preservada: %s",
            filename,
            preparation_error,
        )
        _save_failed_report_entry(
            filename=filename,
            source=source,
            total_chunks=len(chunk_items),
            failed_chunks=all_chunk_indices,
            source_type=source_type,
        )
        return {
            "filename": filename,
            "chunks_count": 0,
            "failed_chunks": len(all_chunk_indices),
            "error": "falha ao preparar ingestao",
        }

    prepared_indices = {int(row["chunk_index"]) for row in chunk_rows}
    missing_indices = sorted(set(all_chunk_indices) - prepared_indices)
    failed_chunks = sorted(set(failed_chunks) | set(missing_indices))
    if failed_chunks or len(chunk_rows) != len(chunk_items):
        logger.error(
            "Preparacao incompleta de %s (%s/%s chunks); versao anterior preservada.",
            filename,
            len(chunk_rows),
            len(chunk_items),
        )
        _save_failed_report_entry(
            filename=filename,
            source=source,
            total_chunks=len(chunk_items),
            failed_chunks=failed_chunks,
            source_type=source_type,
        )
        return {
            "filename": filename,
            "chunks_count": 0,
            "failed_chunks": len(failed_chunks),
            "error": "preparacao incompleta",
        }

    document_row = {
        "id": doc_id,
        "filename": filename,
        "title": title,
        "source": source,
        "doc_type": doc_type,
        "chunk_count": len(chunk_rows),
        "priority": doc_priority,
        "content_hash": content_digest,
        "processing_hash": processing_digest,
    }
    try:
        replaced = _replace_document_atomically(
            filename=filename,
            document_row=document_row,
            section_rows=section_rows,
            chunk_rows=chunk_rows,
            force=force,
        )
    except Exception as persistence_error:
        logger.error(
            "Falha ao trocar %s; transacao revertida e versao anterior preservada: %s",
            filename,
            persistence_error,
        )
        _save_failed_report_entry(
            filename=filename,
            source=source,
            total_chunks=len(chunk_items),
            failed_chunks=all_chunk_indices,
            source_type=source_type,
        )
        return {
            "filename": filename,
            "chunks_count": 0,
            "failed_chunks": len(all_chunk_indices),
            "error": "falha ao substituir documento",
        }

    if not replaced:
        logger.info(
            "Pulando %s: outro processo concluiu a ingestao enquanto esta fonte era preparada.",
            filename,
        )
        return {
            "filename": filename,
            "chunks_count": 0,
            "failed_chunks": 0,
            "skipped": True,
        }

    _save_failed_report_entry(
        filename=filename,
        source=source,
        total_chunks=len(chunk_items),
        failed_chunks=[],
        source_type=source_type,
    )
    logger.info("%s indexado sem pendencias", filename)

    return {
        "filename": filename,
        "chunks_count": len(chunk_rows),
        "failed_chunks": 0,
        "module": module,
        "reused_embeddings": bool(reused_document),
        "content_hash": content_digest,
        "processing_hash": processing_digest,
    }


def ingest_file(
    filepath: str,
    force: bool = False,
    *,
    filename_override: str | None = None,
    docs_root: str | None = None,
) -> dict:
    path = Path(filepath)
    ext = path.suffix.lower()
    docs_root_path = Path(docs_root) if docs_root else None
    document_filename = filename_override or _path_to_document_filename(path, docs_root_path)

    if ext not in READERS:
        logger.warning("Formato nao suportado: %s (%s)", ext, path.name)
        return {
            "filename": document_filename,
            "chunks_count": 0,
            "failed_chunks": 0,
            "error": "formato nao suportado",
        }

    logger.info("Lendo arquivo: %s", path.name)
    try:
        text = READERS[ext](filepath)
    except Exception as e:
        logger.error("Erro lendo %s: %s", path.name, e)
        return {"filename": document_filename, "error": str(e)}

    if not text.strip():
        logger.warning("Arquivo vazio: %s", path.name)
        return {
            "filename": document_filename,
            "chunks_count": 0,
            "failed_chunks": 0,
            "error": "arquivo vazio",
        }

    return _ingest_text_source(
        filename=document_filename,
        title=path.stem,
        source=str(path.resolve()),
        doc_type=ext.lstrip("."),
        text=text,
        source_type="file",
        force=force,
    )


def ingest_url(url: str, force: bool = False) -> dict:
    logger.info("Lendo fonte web source_id=%s", _url_source_id(url))
    text, doc_type, page_title = read_url(url)

    filename = _url_to_filename(url)
    title = page_title or _title_from_url(url)

    return _ingest_text_source(
        filename=filename,
        title=title,
        source=url,
        doc_type=doc_type,
        text=text,
        source_type="url",
        force=force,
    )


def _collect_local_files(
    directory: str | None = None,
    create_if_missing: bool = False,
    recursive: bool | None = None,
) -> tuple[Path, list[Path]]:
    docs_dir = directory if directory is not None else config.DOCS_DIR
    docs_path = Path(docs_dir)

    if not docs_path.exists():
        if create_if_missing:
            docs_path.mkdir(parents=True, exist_ok=True)
            logger.info("Diretorio criado: %s", docs_path)
            logger.info("Coloque seus documentos nele e execute novamente")
        return docs_path, []

    use_recursive = config.INGEST_RECURSIVE if recursive is None else bool(recursive)
    excluded_dirs = config.INGEST_EXCLUDED_DIRS
    iterator = docs_path.rglob("*") if use_recursive else docs_path.iterdir()
    file_sources: list[Path] = []
    for path in iterator:
        if not path.is_file() or path.suffix.lower() not in READERS:
            continue
        relative_parts = path.relative_to(docs_path).parts[:-1]
        if any(part.lower() in excluded_dirs for part in relative_parts):
            continue
        file_sources.append(path)

    deduped_sources: list[Path] = []
    seen: set[str] = set()
    for path in file_sources:
        key = str(path.resolve()).lower()
        if key in seen:
            continue
        seen.add(key)
        deduped_sources.append(path)

    return docs_path, deduped_sources

# --- Ingest Directory e Main mantidos similares ---

def ingest_directory(
    directory: str | None = None,
    retry_failed_only: bool = False,
    force: bool = False,
    urls: list[str] | None = None,
    recursive: bool | None = None,
) -> list[dict]:
    file_sources: list[Path] = []
    url_sources = list(dict.fromkeys([u for u in (urls or []) if _is_url(u)]))
    docs_dir = directory if directory is not None else config.DOCS_DIR
    docs_path = Path(docs_dir)

    if retry_failed_only:
        failed_files, failed_urls = _failed_sources_from_report()
        file_sources = failed_files
        url_sources = list(dict.fromkeys(url_sources + failed_urls))
    else:
        if not docs_path.exists():
            docs_path.mkdir(parents=True, exist_ok=True)
            return []
        _, file_sources = _collect_local_files(
            directory=directory,
            create_if_missing=False,
            recursive=recursive,
        )

    results = []
    for filepath in sorted(file_sources):
        try:
            results.append(
                ingest_file(
                    str(filepath),
                    force=force,
                    filename_override=_path_to_document_filename(filepath, docs_path),
                    docs_root=str(docs_path),
                )
            )
        except Exception as exc:
            logger.error("Erro ao processar %s: %s", filepath.name, exc)

    for url in url_sources:
        try:
            results.append(ingest_url(url, force=force))
        except Exception as exc:
            logger.error(
                "WEB_FETCH_ERROR source_id=%s stage=ingest error_type=%s",
                _url_source_id(url),
                type(exc).__name__,
            )

    return results


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingestao de documentos e URLs para RAG")
    parser.add_argument("directory", nargs="?", default=None, help="Diretorio de documentos")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Reprocessa fontes mesmo quando os hashes nao mudaram",
    )
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument(
        "--recursive",
        dest="recursive",
        action="store_true",
        help="Percorre subdiretorios, respeitando INGEST_EXCLUDED_DIRS",
    )
    parser.add_argument(
        "--no-recursive",
        dest="recursive",
        action="store_false",
        help="Processa somente o diretorio informado",
    )
    parser.set_defaults(recursive=None)
    parser.add_argument("--url", action="append", default=[])
    parser.add_argument("--urls-file", default=None)
    return parser.parse_args()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    config.validate_ai_config()
    validate_database_config()
    args = _parse_args()

    urls = list(args.url or [])
    if args.urls_file:
        urls.extend(_load_urls_from_file(args.urls_file))

    ingest_directory(
        directory=args.directory,
        retry_failed_only=args.retry_failed,
        force=args.force,
        urls=urls,
        recursive=args.recursive,
    )
