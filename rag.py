"""
rag.py — Pipeline RAG: embedding (Gemini) → busca hibrida (Supabase REST) → resposta (Gemini)
Usa HTTP direto com Supabase para evitar dependencias pesadas.
Suporta busca hibrida (vetor + full-text) com Reciprocal Rank Fusion.
"""

import atexit
import base64 as _base64
import json
import logging
import math
import random as _random
import re
import time as _time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import unicodedata

import httpx
from google import genai
from google.genai import types as _gtypes

import config
from bot_common import normalize_text
from db import db_call, db_delete, db_insert, db_select, db_update, is_missing_function_error

logger = logging.getLogger(__name__)

_knowledge_gap_rpc_available: bool | None = None
_top_knowledge_gaps_rpc_available: bool | None = None
_business_rules_cache: tuple[str, float, str] | None = None
_full_context_cache: tuple[str, float] | None = None  # (text, mtime_max)

# Expansao de abreviaturas do dominio para embedding de query.
# Mantem a query original para FTS.
# Ajuste os valores abaixo conforme o glossario interno da operacao.
QUERY_ABBREVIATIONS = {
    "RCA": "representante comercial autonomo",
    "NF": "nota fiscal",
    "NFE": "nota fiscal eletronica",
    "NFC": "nota fiscal de consumidor",
    "FPU": "F P U",
    "MIQ": "M I Q",
    "MQT": "M Q T",
    "SQP": "sistema de quota e premio",
    "PDV": "ponto de venda",
    "SKU": "stock keeping unit",
    "WMS": "warehouse management system",
    "ATUALIZID": "controle de sincronizacao atualizid",
    "USAGRADE": "parametro usagrade de grade de produto",
    "PARAMFILIAL": "rotina paramfilial 132",
    "MXS": "maxima sistemas maxpedido",
}

INTENT_PRIORITY = [
    "sql_lookup",
    "troubleshooting",
    "integration",
    "configuration",
    "process",
    "general",
]

INTENT_KEYWORDS = {
    "sql_lookup": (
        "sql",
        "select",
        "from",
        "join",
        "where",
        "tabela",
        "campo",
        "coluna",
        "query",
        "banco",
        "mxsintegracaopedido",
        "mxsintegracaopedido_log",
        "mxshistoricocritica",
        "mxsparametro",
        "mxsparametrovalor",
        "pcpedcfv",
        "pclientfv",
        "pcpedifv",
    ),
    "configuration": (
        "parametro",
        "configuracao",
        "permissao",
        "habilitar",
        "desabilitar",
        "central",
        "perfil",
        "sincronizacao",
        "usagrade",
        "paramfilial",
        "mxtabela",
    ),
    "integration": (
        "integracao",
        "endpoint",
        "api",
        "erp",
        "statuspedidos",
        "mixintegracaopedido",
        "mxshistoricopedc",
        "webhook",
        "json_envio",
        "json_retorno",
        "status de pedido",
    ),
    "troubleshooting": (
        "erro",
        "falha",
        "nao funciona",
        "nao atualiza",
        "travou",
        "critica",
        "problema",
        "corrigir",
        "ajuda",
        "critica",
        "bloqueado",
        "sincroniza",
    ),
    "process": (
        "pedido",
        "venda",
        "orcamento",
        "pre pedido",
        "filial retira",
        "cliente bloqueado",
        "timeline",
        "roteiro",
        "visita",
        "pre-venda",
        "check in",
        "check out",
    ),
}

QUERY_MODULE_HINTS = {
    "sql_integracao": (
        "sql",
        "select",
        "join",
        "where",
        "tabela",
        "campo",
        "coluna",
        "banco",
        "integracao",
        "endpoint",
        "api",
        "erp",
        "mxsintegracaopedido",
        "mxsintegracaopedido_log",
        "mxshistoricocritica",
        "pcpedcfv",
        "pclientfv",
        "pcpedifv",
    ),
    "parametros_configuracao": (
        "parametro",
        "configuracao",
        "permissao",
        "central",
        "sincronizacao",
        "perfil",
        "usagrade",
        "paramfilial",
        "mxsparametro",
        "mxsparametrovalor",
    ),
    "pedidos_vendas": (
        "pedido",
        "venda",
        "orcamento",
        "cliente bloqueado",
        "filial retira",
        "pre pedido",
        "timeline",
        "check in",
        "check out",
    ),
    "campanhas_descontos": (
        "campanha",
        "desconto",
        "verba",
        "miq",
        "mqt",
        "fpu",
        "sqp",
    ),
    "rotas_visitas_consultas": (
        "rota",
        "roteiro",
        "visita",
        "check in",
        "check out",
    ),
    "financeiro_pagamentos": (
        "financeiro",
        "pagamento",
        "inadimplente",
        "limite",
        "conta corrente",
        "titulos abertos",
        "mxstitulosabertos",
    ),
}

INTENT_DEFAULT_MODULES = {
    "sql_lookup": ["sql_integracao"],
    "integration": ["sql_integracao", "suporte_processos"],
    "configuration": ["parametros_configuracao"],
    "process": ["pedidos_vendas"],
    "troubleshooting": ["suporte_processos", "pedidos_vendas"],
}

INTENT_DOC_TYPES = {
    "sql_lookup": ["md", "pdf", "txt"],
    "integration": ["md", "pdf", "txt", "json"],
    "configuration": ["md", "pdf", "txt"],
    "process": ["md", "pdf", "txt"],
    "troubleshooting": ["md", "pdf", "txt"],
}

INTENT_RESPONSE_INSTRUCTIONS = {
    "sql_lookup": (
        "Para perguntas SQL/tabela, organize em subtitulos curtos: "
        "objetivo, tabelas principais, campos-chave e validacao. "
        "Explique o uso em paragrafos curtos e reserve bullets para campos ou validacoes. "
        "Inclua SQL somente se estiver no contexto."
    ),
    "configuration": (
        "Para configuracoes, responda em texto corrido com subtitulos. "
        "Traga caminho exato (menu/tela/campo) quando houver, parametros envolvidos "
        "e impacto esperado."
    ),
    "integration": (
        "Para integracao, explique primeiro o fluxo origem-destino em um paragrafo curto. "
        "Depois destaque tabelas/enderecos envolvidos e pontos de validacao operacional "
        "em grupos compactos."
    ),
    "troubleshooting": (
        "Para troubleshooting, organize em causa provavel, verificacoes e acao recomendada. "
        "Use checklist apenas nas verificacoes praticas; explique a causa e a conclusao em paragrafos curtos."
    ),
    "process": (
        "Para processo de negocio, explique as pre-condicoes em texto e use passo a passo curto "
        "somente quando a ordem estiver documentada."
    ),
}

_CITATION_INLINE_RE = re.compile(r"\[fonte:\s*([^\]]+)\]", flags=re.IGNORECASE)
_SOURCES_SECTION_RE = re.compile(
    r"^\s*(?:#{1,6}\s*)?(?:\*\*|__)?\s*fontes?\s*:?\s*(?:\*\*|__)?\s*:?\s*$",
    flags=re.IGNORECASE | re.MULTILINE,
)
_FACTUAL_LINE_RE = re.compile(
    r"(?:\b(select|update|insert|delete|from|join|where|tabela|campo|coluna|menu|tela|parametro|rotina|erro|codigo)\b|\d)",
    flags=re.IGNORECASE,
)
_OPERATIONAL_QUERY_RE = re.compile(
    r"\b(menu|tela|campo|parametro|sql|select|where|tabela|coluna|passo|rotina|erro|integracao)\b",
    flags=re.IGNORECASE,
)
_REFORMULATION_CLARIFY_RE = re.compile(
    r"^\s*(pode|poderia|consigo|precisa|precisamos|favor)\b.*\b(detalhar|informar|enviar|explicar)\b",
    flags=re.IGNORECASE,
)

_PROVIDER_ERROR_MESSAGES = frozenset(
    {
        "Nao foi possivel extrair uma resposta do modelo.",
        "O servico esta sobrecarregado no momento. Tente novamente em alguns segundos.",
        "Erro de configuracao do bot. Contate o administrador.",
        "A consulta demorou demais. Tente reformular com uma pergunta mais curta.",
        "Nao foi possivel conectar ao servico. Tente novamente em instantes.",
        "Ocorreu um erro inesperado. Tente novamente.",
    }
)

# ── Clientes ──────────────────────────────────────────────
_gemini_generation: genai.Client | None = None
_gemini_embeddings: genai.Client | None = None
_http_client: httpx.Client | None = None


class _GeneratedTextResponse:
    """Compatibilidade para trechos que esperam objeto com atributo .text."""

    def __init__(self, text: str):
        self.text = text or ""


class _ProviderErrorResponse(str):
    """Texto seguro para o usuario que representa uma falha do provider."""


def _provider_error_response(message: str) -> _ProviderErrorResponse:
    return _ProviderErrorResponse(message)


def _is_provider_error_response(answer: str) -> bool:
    return (
        isinstance(answer, _ProviderErrorResponse)
        or str(answer or "").strip() in _PROVIDER_ERROR_MESSAGES
    )


def _active_llm_provider() -> str:
    provider = (config.GENERATION_PROVIDER or "gemini").strip().lower()
    return provider if provider in {"gemini", "openai"} else "gemini"


def _active_embedding_provider() -> str:
    provider = (config.EMBEDDING_PROVIDER or "gemini").strip().lower()
    return provider if provider in {"gemini", "openai"} else "gemini"


def _resolve_text_model(requested_model: str | None, *, purpose: str = "general") -> str:
    provider = _active_llm_provider()
    requested = (requested_model or "").strip()
    requested_lower = requested.lower()

    if provider == "openai":
        if requested and not requested_lower.startswith("gemini"):
            return requested
        if purpose == "reformulation":
            return config.OPENAI_REFORMULATION_MODEL or config.GENERATION_MODEL
        if purpose == "contextual":
            return config.OPENAI_CONTEXTUAL_MODEL or config.OPENAI_REFORMULATION_MODEL or config.GENERATION_MODEL
        return config.GENERATION_MODEL

    # Provider Gemini: evita usar modelo OpenAI por engano.
    if requested and not requested_lower.startswith("gpt-") and not requested_lower.startswith("o"):
        return requested
    if purpose == "reformulation":
        return config.REFORMULATION_MODEL
    if purpose == "contextual":
        return config.CONTEXTUAL_RETRIEVAL_MODEL
    return config.GENERATION_MODEL


def _resolve_embedding_model() -> str:
    return (config.EMBEDDING_MODEL or "").strip()


def get_gemini() -> genai.Client:
    global _gemini_generation
    if _gemini_generation is None:
        _gemini_generation = genai.Client(api_key=config.GENERATION_API_KEY)
    return _gemini_generation


def get_gemini_embeddings() -> genai.Client:
    global _gemini_embeddings
    if _gemini_embeddings is None:
        _gemini_embeddings = genai.Client(api_key=config.EMBEDDING_API_KEY)
    return _gemini_embeddings


def _openai_headers(api_key: str | None, setting_name: str) -> dict[str, str]:
    if not api_key:
        raise EnvironmentError(f"{setting_name} nao configurada.")
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _openai_url(base_url: str, path: str) -> str:
    base = base_url.rstrip("/")
    clean_path = path.lstrip("/")
    return f"{base}/{clean_path}"


def _openai_extract_text(
    message_content,
    *,
    message: dict | None = None,
    raw_response: dict | None = None,
) -> str:
    def _extract_text_value(value) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            candidate = value.get("value")
            if isinstance(candidate, str):
                return candidate
            candidate = value.get("text")
            if isinstance(candidate, str):
                return candidate
        return ""

    def _extract_refusal_value(value) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            candidate = value.get("value")
            if isinstance(candidate, str):
                return candidate
            candidate = value.get("text")
            if isinstance(candidate, str):
                return candidate
        return ""

    if isinstance(message_content, str):
        return message_content.strip()

    if isinstance(message_content, dict):
        nested = _extract_text_value(message_content.get("text"))
        if nested:
            return nested.strip()
        refusal = _extract_refusal_value(message_content.get("refusal"))
        if refusal:
            return refusal.strip()

    if isinstance(message_content, list):
        parts: list[str] = []
        for item in message_content:
            if isinstance(item, str):
                if item.strip():
                    parts.append(item.strip())
                continue
            if not isinstance(item, dict):
                continue
            item_type = str(item.get("type") or "").lower()
            if item_type in {"text", "output_text"}:
                extracted = _extract_text_value(item.get("text"))
                if extracted:
                    parts.append(extracted.strip())
            elif item_type == "refusal":
                extracted = _extract_refusal_value(item.get("refusal"))
                if extracted:
                    parts.append(extracted.strip())
        joined = "\n".join(part for part in parts if part).strip()
        if joined:
            return joined

    if isinstance(message, dict):
        refusal = _extract_refusal_value(message.get("refusal"))
        if refusal:
            return refusal.strip()
        content_from_message = message.get("content")
        if content_from_message is not message_content:
            extracted = _openai_extract_text(content_from_message)
            if extracted:
                return extracted.strip()

    if isinstance(raw_response, dict):
        output_text = raw_response.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()
        if isinstance(output_text, list):
            joined = "\n".join(str(item).strip() for item in output_text if str(item).strip()).strip()
            if joined:
                return joined
        choices = raw_response.get("choices") or []
        if choices:
            first_choice = choices[0] if isinstance(choices[0], dict) else {}
            choice_text = first_choice.get("text")
            if isinstance(choice_text, str) and choice_text.strip():
                return choice_text.strip()

    return ""


def _openai_chat_generate(
    *,
    model: str,
    messages: list[dict],
    max_tokens: int = 2048,
) -> _GeneratedTextResponse:
    payload = {
        "model": model,
        "messages": messages,
        "max_completion_tokens": max_tokens,
    }
    resp = _get_http_client().post(
        _openai_url(config.GENERATION_BASE_URL, "/chat/completions"),
        headers=_openai_headers(config.GENERATION_API_KEY, "GENERATION_API_KEY"),
        json=payload,
        timeout=120,
    )
    if resp.status_code == 400 and "max_completion_tokens" in (resp.text or "").lower():
        payload.pop("max_completion_tokens", None)
        payload["max_tokens"] = max_tokens
        resp = _get_http_client().post(
            _openai_url(config.GENERATION_BASE_URL, "/chat/completions"),
            headers=_openai_headers(config.GENERATION_API_KEY, "GENERATION_API_KEY"),
            json=payload,
            timeout=120,
        )
    if resp.status_code >= 400:
        logger.error(
            "OpenAI CHAT erro %s (model=%s): %s",
            resp.status_code,
            model,
            resp.text[:2000],
        )
    resp.raise_for_status()
    data = resp.json()

    choices = data.get("choices") or []
    if not choices:
        extracted = _openai_extract_text(None, raw_response=data)
        return _GeneratedTextResponse(extracted)

    first_choice = choices[0] if isinstance(choices[0], dict) else {}
    message = first_choice.get("message") or {}
    extracted = _openai_extract_text(
        message.get("content"),
        message=message,
        raw_response=data,
    )
    if not extracted:
        logger.warning(
            "OpenAI CHAT retornou texto vazio (model=%s, finish_reason=%s).",
            model,
            first_choice.get("finish_reason"),
        )
    return _GeneratedTextResponse(extracted)


def _gemini_generate(
    model: str,
    *,
    system: str | None = None,
    contents,
    max_tokens: int = 2048,
):
    """
    Wrapper retrocompativel de geracao:
    - provider=gemini -> Gemini SDK
    - provider=openai -> Chat Completions
    """
    purpose = "general"
    if model == config.REFORMULATION_MODEL:
        purpose = "reformulation"
    elif model == config.CONTEXTUAL_RETRIEVAL_MODEL:
        purpose = "contextual"

    provider = _active_llm_provider()
    if provider == "openai":
        resolved_model = _resolve_text_model(
            model,
            purpose=purpose,
        )
        user_content = contents if isinstance(contents, str) else str(contents)
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user_content})
        return _openai_chat_generate(
            model=resolved_model,
            messages=messages,
            max_tokens=max_tokens,
        )

    resolved_model = _resolve_text_model(
        model,
        purpose=purpose,
    )
    cfg = _gtypes.GenerateContentConfig(max_output_tokens=max_tokens)
    if system:
        cfg.system_instruction = system
    response = get_gemini().models.generate_content(
        model=resolved_model,
        contents=contents,
        config=cfg,
    )
    return response


def _anthropic_msgs_to_gemini(messages: list[dict]) -> list[_gtypes.Content]:
    """Converte historico no formato Anthropic (role/content) para Gemini Content."""
    result = []
    for msg in messages:
        role = "model" if msg["role"] == "assistant" else "user"
        content = msg.get("content", "")
        if isinstance(content, str):
            parts = [_gtypes.Part(text=content)]
        elif isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, str):
                    parts.append(_gtypes.Part(text=block))
                elif isinstance(block, dict):
                    if block.get("type") == "text":
                        parts.append(_gtypes.Part(text=block["text"]))
                    elif block.get("type") == "image":
                        src = block["source"]
                        parts.append(_gtypes.Part(
                            inline_data=_gtypes.Blob(
                                mime_type=src["media_type"],
                                data=_base64.b64decode(src["data"]),
                            )
                        ))
        else:
            parts = [_gtypes.Part(text=str(content))]
        result.append(_gtypes.Content(role=role, parts=parts))
    return result


def _get_http_client() -> httpx.Client:
    """Retorna httpx.Client reutilizavel com connection pooling."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.Client(
            timeout=60,
            trust_env=False,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        atexit.register(_close_http_client)
    return _http_client


def _close_http_client():
    """Fecha o httpx client no shutdown do processo."""
    global _http_client
    if _http_client is not None:
        try:
            _http_client.close()
        except Exception:
            logger.debug("Erro ao fechar http client no shutdown", exc_info=True)
        _http_client = None


# ── Retry para erros transientes ─────────────────────────
def _retry_on_transient(fn, max_retries: int = 2, backoff: float = 1.0):
    """Retenta chamadas HTTP em erros transientes (429, 502, 503, 504) com backoff exponencial."""
    for attempt in range(max_retries + 1):
        try:
            return fn()
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (429, 502, 503, 504) and attempt < max_retries:
                delay = backoff * (2 ** attempt) + _random.uniform(0, 1.0)
                logger.warning(
                    "Erro transiente %s, tentativa %s/%s. Aguardando %.1fs...",
                    e.response.status_code,
                    attempt + 1,
                    max_retries,
                    delay,
                )
                _time.sleep(delay)
                continue
            raise


# ── Supabase REST helpers ─────────────────────────────────
def db_insert_rows(table: str, data: dict | list) -> list:
    return db_insert(table, data)


def db_select_rows(table: str, select: str = "*", filters: dict = None) -> list:
    return db_select(table, columns=select, filters=filters)


def db_delete_rows(table: str, column: str, value: str) -> None:
    db_delete(table, {column: f"eq.{value}"})


def db_update_rows(table: str, data: dict, filters: dict) -> list:
    return db_update(table, data, filters)


_VOID_DB_FUNCTIONS = {
    "approve_feedback",
    "reject_feedback",
    "upsert_knowledge_gap",
}


def db_call_rows(function_name: str, params: dict, expect_rows: bool = True) -> list:
    if function_name in _VOID_DB_FUNCTIONS:
        expect_rows = False
    return db_call(function_name, params, expect_rows=expect_rows)


def _is_missing_rpc_function(error: Exception, function_name: str) -> bool:
    return is_missing_function_error(error)


def supabase_insert(table: str, data: dict | list) -> list:
    return db_insert_rows(table, data)


def supabase_select(table: str, select: str = "*", filters: dict = None) -> list:
    return db_select_rows(table, select=select, filters=filters)


def supabase_delete(table: str, column: str, value: str) -> None:
    db_delete_rows(table, column, value)


def supabase_update(table: str, data: dict, filters: dict) -> list:
    return db_update_rows(table, data, filters)


def supabase_rpc(function_name: str, params: dict) -> list:
    return db_call_rows(function_name, params)


def _safe_similarity(value: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(parsed):
        return 0.0
    return parsed


def _new_query_id() -> str:
    return str(uuid.uuid4())


def _normalize_source_name(value: str) -> str:
    return (value or "").strip().strip("`* ").lower()


def _extract_cited_sources(answer: str) -> set[str]:
    cited: set[str] = set()

    for match in _CITATION_INLINE_RE.findall(answer or ""):
        for source in re.split(r"[;,|]", match):
            source = _normalize_source_name(source)
            if source:
                cited.add(source)

    in_sources_section = False
    for raw_line in (answer or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _SOURCES_SECTION_RE.match(line):
            in_sources_section = True
            continue
        if in_sources_section:
            source_item = re.match(r"^(?:[-*•]\s+|\d+[.)]\s+)(.+)$", line)
            if source_item:
                source = _normalize_source_name(source_item.group(1).split("(", 1)[0])
                if source:
                    cited.add(source)
                continue
            # encerra secao ao bater em outro cabecalho/paragraph
            if re.match(r"^[A-Za-z].*:$", line):
                in_sources_section = False
                continue

    return cited


def _strip_sources_section(answer: str) -> str:
    text = (answer or "").strip()
    if not text:
        return ""
    match = _SOURCES_SECTION_RE.search(text)
    if not match:
        return text
    return text[:match.start()].rstrip()


def _enforce_sources_section_only(
    answer: str,
    *,
    allowed_sources: set[str] | None,
    source_display_map: dict[str, str] | None = None,
) -> tuple[str, set[str]]:
    text = (answer or "").strip()
    if not text:
        return "", set()
    if text.startswith(config.NO_ANSWER_PHRASE):
        return text, set()

    body = _strip_sources_section(text)
    body = _CITATION_INLINE_RE.sub("", body)
    body = re.sub(r"[ \t]+(\n)", r"\1", body)
    body = re.sub(r"[ \t]{2,}", " ", body)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()

    cited_sources = _extract_cited_sources(text)
    if allowed_sources:
        cited_sources = {source for source in cited_sources if source in allowed_sources}
    if not cited_sources:
        return body, set()

    sources_lines: list[str] = []
    for source in sorted(cited_sources):
        display_name = (source_display_map or {}).get(source, source)
        sources_lines.append(f"- {display_name}")
    sources_block = "Fontes:\n" + "\n".join(sources_lines)

    if body:
        return f"{body}\n\n{sources_block}", cited_sources
    return sources_block, cited_sources


def _line_has_citation(line: str) -> bool:
    return bool(_CITATION_INLINE_RE.search(line))


def _is_operational_specific_query(question: str) -> bool:
    return bool(_OPERATIONAL_QUERY_RE.search(question or ""))


def _looks_like_clarifying_request(text: str) -> bool:
    normalized = normalize_text(text or "")
    if not normalized:
        return False
    return bool(_REFORMULATION_CLARIFY_RE.search(text or "")) or normalized.startswith(
        "pode detalhar"
    )


def _build_clarifying_question(question: str) -> str:
    normalized = normalize_text(question or "")
    if any(token in normalized for token in ("sql", "tabela", "campo", "coluna", "select")):
        return "Pode informar o nome da tabela/campo ou um trecho da query que voce espera consultar?"
    if any(token in normalized for token in ("erro", "critica", "falha", "codigo")):
        return "Pode enviar a mensagem de erro completa e, se possivel, um print da tela?"
    if any(token in normalized for token in ("parametro", "configuracao", "menu", "tela")):
        return "Pode informar o nome exato do parametro/tela e em qual modulo voce esta?"
    return config.ABSTAIN_CLARIFYING_QUESTION


def _build_abstain_response(question: str) -> str:
    return (
        f"{config.NO_ANSWER_PHRASE}\n\n"
        f"Pergunta de esclarecimento: {_build_clarifying_question(question)}"
    )


def _validate_grounded_answer(
    *,
    answer: str,
    allowed_sources: set[str],
    question: str,
    require_sources_section: bool,
) -> tuple[bool, list[str], set[str]]:
    if not answer:
        return False, ["Resposta vazia."], set()
    if normalize_text(answer).startswith("nao foi possivel extrair uma resposta do modelo"):
        return False, ["Resposta vazia."], set()

    if answer.strip().startswith(config.NO_ANSWER_PHRASE):
        return True, [], set()

    errors: list[str] = []
    cited_sources = _extract_cited_sources(answer)

    if require_sources_section and not _SOURCES_SECTION_RE.search(answer):
        errors.append("Resposta sem secao 'Fontes:'.")

    if not cited_sources:
        errors.append("Resposta sem citacoes de fonte.")

    unknown_sources = {s for s in cited_sources if s not in allowed_sources}
    if unknown_sources:
        errors.append(f"Fontes nao recuperadas no contexto: {', '.join(sorted(unknown_sources))}.")

    if _is_operational_specific_query(question) and not cited_sources:
        errors.append("Pergunta operacional exige fonte explicita.")

    return len(errors) == 0, errors, cited_sources


def _is_grounding_error_critical(errors: list[str]) -> bool:
    """Define se falha de grounding exige abstencao obrigatoria."""
    if not errors:
        return False

    for error in errors:
        normalized = normalize_text(error or "")
        if "afirmacoes factuais sem citacao inline" in normalized:
            continue
        if "resposta vazia" in normalized:
            return True
        if "fontes nao recuperadas no contexto" in normalized:
            return True
        if "pergunta operacional exige fonte explicita" in normalized:
            return True
        return True
    return False


def _summarize_chunks_for_trace(chunks: list[dict]) -> dict[str, Any]:
    safe_chunks = chunks or []
    top_similarity = max(
        (
            _safe_similarity(
                chunk.get("vector_similarity", chunk.get("similarity", 0.0))
            )
            for chunk in safe_chunks
        ),
        default=0.0,
    )
    top_lexical_score = max(
        (_safe_similarity(chunk.get("lexical_score", 0.0)) for chunk in safe_chunks),
        default=0.0,
    )
    top_fusion_score = max(
        (_safe_similarity(chunk.get("fusion_score", 0.0)) for chunk in safe_chunks),
        default=0.0,
    )
    top_feedback_priority = max(
        (_safe_similarity(chunk.get("feedback_priority", 0.0)) for chunk in safe_chunks),
        default=0.0,
    )
    filenames = [
        str(chunk.get("filename"))
        for chunk in safe_chunks
        if chunk.get("filename")
    ]
    section_ids = {
        str(chunk.get("section_id"))
        for chunk in safe_chunks
        if chunk.get("section_id")
    }
    document_ids = {
        str(chunk.get("document_id") or chunk.get("filename") or "")
        for chunk in safe_chunks
        if chunk.get("document_id") or chunk.get("filename")
    }
    return {
        "top_similarity": top_similarity,
        "top_vector_similarity": top_similarity,
        "top_lexical_score": top_lexical_score,
        "top_fusion_score": top_fusion_score,
        "top_feedback_priority": top_feedback_priority,
        "retrieval_origins": sorted(
            {
                str(chunk.get("retrieval_origin"))
                for chunk in safe_chunks
                if chunk.get("retrieval_origin")
            }
        ),
        "retrieved_chunk_count": len(safe_chunks),
        "retrieved_sources": sorted(set(filenames)),
        "retrieved_section_count": len(section_ids),
        "retrieved_document_count": len(document_ids),
    }


def _log_ask_trace(trace: dict[str, Any]) -> None:
    try:
        logger.info("ASK_TRACE %s", json.dumps(trace, ensure_ascii=False))
    except Exception:
        logger.info("ASK_TRACE %s", trace)


def _set_response_state(
    trace: dict[str, Any],
    state: str,
    *,
    citation_syntax: str,
    semantic_support: str,
) -> None:
    trace["response_state"] = state
    trace["citation_validation"] = {
        "syntax": citation_syntax,
        "semantic_support": semantic_support,
    }


def get_model_config() -> dict[str, str]:
    """Resumo do provider/modelos ativos para exibicao e diagnostico."""
    return {
        "llm_provider": _active_llm_provider(),
        "embedding_provider": _active_embedding_provider(),
        "generation_model": _resolve_text_model(config.GENERATION_MODEL, purpose="general"),
        "reformulation_model": _resolve_text_model(
            config.REFORMULATION_MODEL,
            purpose="reformulation",
        ),
        "contextual_model": _resolve_text_model(
            config.CONTEXTUAL_RETRIEVAL_MODEL,
            purpose="contextual",
        ),
        "reranker_model": _resolve_text_model(
            config.RERANKER_MODEL,
            purpose="reformulation",
        ),
        "embedding_model": _resolve_embedding_model(),
    }


def _fallback_log_knowledge_gap(query: str, max_similarity: float, platform: str) -> None:
    normalized_query = query[:500]
    similarity = _safe_similarity(max_similarity)

    rows = supabase_select(
        "knowledge_gaps",
        select="id,occurrences,max_similarity",
        filters={"query": f"eq.{normalized_query}", "limit": "1"},
    )

    if rows:
        row = rows[0]
        row_id = row.get("id")
        if row_id:
            now_iso = datetime.now(timezone.utc).isoformat()
            supabase_update(
                "knowledge_gaps",
                {
                    "occurrences": int(row.get("occurrences", 0) or 0) + 1,
                    "max_similarity": max(
                        _safe_similarity(row.get("max_similarity", 0.0)),
                        similarity,
                    ),
                    "platform": platform,
                    "last_seen": now_iso,
                },
                {"id": f"eq.{row_id}"},
            )
            return

    supabase_insert(
        "knowledge_gaps",
        {
            "query": normalized_query,
            "max_similarity": similarity,
            "platform": platform,
            "occurrences": 1,
        },
    )


def _fallback_get_top_knowledge_gaps(limit: int) -> list[dict]:
    safe_limit = max(1, min(int(limit), 100))
    return supabase_select(
        "knowledge_gaps",
        select="query,occurrences,max_similarity,platform,last_seen",
        filters={
            "resolved": "eq.false",
            "order": "occurrences.desc,last_seen.desc",
            "limit": safe_limit,
        },
    )


# ── Embedding (Google Gemini - GRATIS) ────────────────────
def _normalize_embedding(values: list[float]) -> list[float]:
    values = [v if math.isfinite(v) else 0.0 for v in values]
    target_dims = config.EMBEDDING_DIMENSIONS
    if len(values) > target_dims:
        logger.warning(
            "Embedding maior que o esperado (%s > %s); truncando.",
            len(values),
            target_dims,
        )
        values = values[:target_dims]
    elif len(values) < target_dims:
        logger.warning(
            "Embedding menor que o esperado (%s < %s); preenchendo com zeros.",
            len(values),
            target_dims,
        )
        values.extend([0.0] * (target_dims - len(values)))
    return values


def _openai_create_embeddings(contents: list[str], model: str) -> list[list[float]]:
    payload: dict[str, Any] = {
        "model": model,
        "input": contents,
    }
    if model.startswith("text-embedding-3"):
        payload["dimensions"] = config.EMBEDDING_DIMENSIONS

    resp = _get_http_client().post(
        _openai_url(config.EMBEDDING_BASE_URL, "/embeddings"),
        headers=_openai_headers(config.EMBEDDING_API_KEY, "EMBEDDING_API_KEY"),
        json=payload,
        timeout=120,
    )
    if resp.status_code >= 400:
        logger.error(
            "OpenAI EMBEDDINGS erro %s (model=%s): %s",
            resp.status_code,
            model,
            resp.text[:2000],
        )
    resp.raise_for_status()
    data = resp.json()

    rows = data.get("data") or []
    rows = sorted(rows, key=lambda row: int(row.get("index", 0)))
    vectors = []
    for row in rows:
        embedding = row.get("embedding") or []
        vectors.append(_normalize_embedding([float(v) for v in embedding]))
    return vectors


def create_embeddings(contents: list[str], task_type: str = "RETRIEVAL_DOCUMENT") -> list[list[float]]:
    if not contents:
        return []

    provider = _active_embedding_provider()
    model = _resolve_embedding_model()

    if provider == "openai":
        vectors = _openai_create_embeddings(contents, model)
        if len(vectors) != len(contents):
            raise ValueError(
                f"Quantidade de embeddings inconsistente: esperado {len(contents)}, obtido {len(vectors)}."
            )
        return vectors

    payload = contents if len(contents) > 1 else contents[0]
    result = get_gemini_embeddings().models.embed_content(
        model=model,
        contents=payload,
        config={
            "task_type": task_type,
            "output_dimensionality": config.EMBEDDING_DIMENSIONS,
        },
    )

    embeddings = getattr(result, "embeddings", None) or []
    if not embeddings and len(contents) == 1:
        single_embedding = getattr(result, "embedding", None)
        if single_embedding is not None:
            embeddings = [single_embedding]
    vectors = [_normalize_embedding([float(v) for v in emb.values]) for emb in embeddings]
    if len(vectors) != len(contents):
        raise ValueError(
            f"Quantidade de embeddings inconsistente: esperado {len(contents)}, obtido {len(vectors)}."
        )
    return vectors


def create_embedding(text: str, task_type: str = "RETRIEVAL_DOCUMENT") -> list[float]:
    return create_embeddings([text], task_type=task_type)[0]


def create_query_embedding(text: str) -> list[float]:
    return create_embedding(text, task_type="RETRIEVAL_QUERY")


def create_document_embedding(text: str) -> list[float]:
    return create_embedding(text, task_type="RETRIEVAL_DOCUMENT")


def create_document_embeddings(texts: list[str]) -> list[list[float]]:
    return create_embeddings(texts, task_type="RETRIEVAL_DOCUMENT")


def embedding_to_pgvector(embedding: list[float]) -> str:
    """
    Converte lista de floats para o formato string que o pgvector aceita via PostgREST.
    Formato: '[0.1,0.2,0.3,...]'
    Sanitiza valores NaN/Inf para 0.0.
    """
    sanitized = [v if math.isfinite(v) else 0.0 for v in embedding]
    return "[" + ",".join(str(v) for v in sanitized) + "]"


# ── Cache de query embeddings (LRU com TTL) ──────────────
class _TTLCache:
    """Cache LRU com TTL. Evicao O(1) via OrderedDict."""

    def __init__(self, maxsize: int = 500, ttl: float = 3600.0):
        self._cache: OrderedDict[str, tuple[list[float], float]] = OrderedDict()
        self._maxsize = maxsize
        self._ttl = ttl

    def get(self, key: str) -> list[float] | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        value, ts = entry
        if (_time.monotonic() - ts) >= self._ttl:
            del self._cache[key]
            return None
        self._cache.move_to_end(key)
        return value

    def put(self, key: str, value: list[float]) -> None:
        self._cache[key] = (value, _time.monotonic())
        self._cache.move_to_end(key)
        while len(self._cache) > self._maxsize:
            self._cache.popitem(last=False)  # O(1) — remove o mais antigo


_query_embedding_cache = _TTLCache(maxsize=500, ttl=3600.0)


def _get_cached_query_embedding(query: str) -> list[float]:
    """Retorna embedding de query com cache in-memory (TTL 1h, LRU)."""
    cached = _query_embedding_cache.get(query)
    if cached is not None:
        logger.debug("Cache hit para query embedding: '%s'", query[:60])
        return cached

    embedding = create_query_embedding(query)
    _query_embedding_cache.put(query, embedding)
    return embedding


def _preprocess_query(query: str) -> tuple[str, str]:
    """Retorna (query_para_embedding, query_para_fts)."""
    query_for_fts = query
    query_for_embedding = query

    for abbreviation, expansion in QUERY_ABBREVIATIONS.items():
        pattern = rf"\b{re.escape(abbreviation)}\b"
        query_for_embedding = re.sub(
            pattern,
            lambda m: f"{m.group(0)} {expansion}",
            query_for_embedding,
            flags=re.IGNORECASE,
        )

    query_for_embedding = re.sub(r"\s+", " ", query_for_embedding).strip() or query
    return query_for_embedding, query_for_fts


def _normalize_route_text(value: str) -> str:
    return f" {normalize_text(value)} "


def _classify_query_intent(query: str) -> dict:
    if not config.RAG_ENABLE_INTENT_ROUTING:
        return {"intent": "general", "doc_types": [], "modules": []}

    normalized = _normalize_route_text(query)
    scores: dict[str, int] = {}

    for intent, keywords in INTENT_KEYWORDS.items():
        score = 0
        for keyword in keywords:
            normalized_keyword = _normalize_route_text(keyword).strip()
            if f" {normalized_keyword} " in normalized:
                score += 1
        if score > 0:
            scores[intent] = score

    if scores:
        best_score = max(scores.values())
        tied = {intent for intent, score in scores.items() if score == best_score}
        intent = next((candidate for candidate in INTENT_PRIORITY if candidate in tied), "general")
    else:
        intent = "general"

    modules: set[str] = set(INTENT_DEFAULT_MODULES.get(intent, []))
    for module, keywords in QUERY_MODULE_HINTS.items():
        for keyword in keywords:
            normalized_keyword = _normalize_route_text(keyword).strip()
            if f" {normalized_keyword} " in normalized:
                modules.add(module)
                break

    doc_types = list(INTENT_DOC_TYPES.get(intent, []))
    return {
        "intent": intent,
        "doc_types": doc_types,
        "modules": sorted(modules),
    }


def _build_search_filters(query_plan: dict | None) -> tuple[list[str] | None, list[str] | None]:
    if not query_plan:
        return None, None

    doc_types = query_plan.get("doc_types") if config.RAG_FILTER_BY_DOC_TYPE else None
    modules = query_plan.get("modules") if config.RAG_FILTER_BY_MODULE else None

    normalized_doc_types = [str(v).lower() for v in (doc_types or []) if str(v).strip()]
    normalized_modules = [str(v).lower() for v in (modules or []) if str(v).strip()]

    return normalized_doc_types or None, normalized_modules or None


def _search_rpc_with_filter_fallback(function_name: str, params: dict) -> list:
    try:
        return _retry_on_transient(lambda: supabase_rpc(function_name, params))
    except Exception as e:
        extra_params = {
            "filter_doc_types",
            "filter_modules",
            "filter_section_ids",
            "fetch_limit",
        }
        has_optional_params = any(key in params for key in extra_params)
        if has_optional_params and _is_missing_rpc_function(e, function_name):
            fallback_params = {
                key: value
                for key, value in params.items()
                if key not in extra_params
            }
            logger.warning(
                "Funcao %s ainda sem suporte a filtros/argumentos opcionais; repetindo sem eles.",
                function_name,
            )
            return _retry_on_transient(lambda: supabase_rpc(function_name, fallback_params))
        raise


def _load_business_rules_context() -> str:
    if not config.RAG_ENABLE_BUSINESS_RULES:
        return ""

    rules_path = Path(config.BUSINESS_RULES_FILE)
    if not rules_path.exists() or not rules_path.is_file():
        return ""

    resolved_path = str(rules_path.resolve())
    mtime = rules_path.stat().st_mtime
    global _business_rules_cache

    if _business_rules_cache:
        cached_path, cached_mtime, cached_text = _business_rules_cache
        if cached_path == resolved_path and cached_mtime == mtime:
            return cached_text

    text = rules_path.read_text(encoding="utf-8", errors="ignore").strip()
    if not text:
        return ""

    max_chars = max(500, int(config.BUSINESS_RULES_MAX_CHARS))
    if len(text) > max_chars:
        logger.warning(
            "Arquivo de regras de negocio excede limite (%s chars). Truncando para %s.",
            len(text),
            max_chars,
        )
        text = text[:max_chars]

    _business_rules_cache = (resolved_path, mtime, text)
    return text


def _load_full_context_docs() -> str:
    """Carrega todos os documentos do diretorio raiz como contexto completo (estilo Claude Projects).

    Retorna o texto concatenado de todos os documentos, com marcadores de documento.
    Usa cache em memoria e recarrega somente se algum arquivo mudou.
    """
    if not config.FULL_CONTEXT_ENABLED:
        return ""

    global _full_context_cache
    docs_dir = Path(config.DOCS_DIR)
    if not docs_dir.exists() or not docs_dir.is_dir():
        logger.warning("FULL_CONTEXT: Diretorio de documentos nao encontrado: %s", docs_dir)
        return ""

    allowed_exts = {ext.strip().lower() for ext in config.FULL_CONTEXT_EXTENSIONS}
    doc_files = sorted(
        f for f in docs_dir.iterdir()
        if f.is_file() and f.suffix.lower() in allowed_exts
    )

    if not doc_files:
        logger.warning("FULL_CONTEXT: Nenhum documento encontrado em %s", docs_dir)
        return ""

    current_mtime_max = max(f.stat().st_mtime for f in doc_files)
    if _full_context_cache:
        cached_text, cached_mtime = _full_context_cache
        if cached_mtime == current_mtime_max:
            return cached_text

    parts: list[str] = []
    total_chars = 0
    max_chars = config.FULL_CONTEXT_MAX_CHARS

    for doc_file in doc_files:
        try:
            content = doc_file.read_text(encoding="utf-8", errors="ignore").strip()
        except Exception as e:
            logger.warning("FULL_CONTEXT: Erro ao ler %s: %s", doc_file.name, e)
            continue

        if not content:
            continue

        if total_chars + len(content) > max_chars:
            remaining = max_chars - total_chars
            if remaining > 1000:
                content = content[:remaining]
                logger.warning(
                    "FULL_CONTEXT: Documento %s truncado para caber no limite.", doc_file.name
                )
            else:
                logger.warning(
                    "FULL_CONTEXT: Limite de %d chars atingido. %s ignorado.",
                    max_chars, doc_file.name,
                )
                break

        parts.append(
            f"<document source=\"{doc_file.name}\">\n"
            f"{content}\n"
            f"</document>"
        )
        total_chars += len(content)

    full_text = "\n\n".join(parts)
    _full_context_cache = (full_text, current_mtime_max)
    logger.info(
        "FULL_CONTEXT: %d documentos carregados (%d chars total).",
        len(parts), total_chars,
    )
    return full_text


def _intent_response_instruction(query_plan: dict | None) -> str:
    if not query_plan:
        return ""
    intent = str(query_plan.get("intent") or "general")
    return INTENT_RESPONSE_INSTRUCTIONS.get(intent, "")


def _postprocess_search_results(
    chunks: list[dict],
    max_results: int,
    threshold: float,
    *,
    max_candidates: int | None = None,
    ranking_mode: str = "vector",
) -> list[dict]:
    if not chunks:
        return []

    has_retrieval_contract = ranking_mode == "retrieval" and any(
        chunk.get("retrieval_rank") is not None
        for chunk in chunks
    )
    if has_retrieval_contract:
        # A RPC ja devolve seeds em ordem de retrieval e vizinhos associados logo depois.
        # similarity permanece apenas como compatibilidade para similaridade vetorial.
        filtered = list(chunks)
    else:
        min_similarity = threshold * config.SIMILARITY_FLOOR_FACTOR
        filtered = [
            chunk
            for chunk in chunks
            if _safe_similarity(chunk.get("similarity", 0)) >= min_similarity
        ]
        filtered.sort(
            key=lambda chunk: _safe_similarity(chunk.get("similarity", 0)),
            reverse=True,
        )

    max_with_neighbors = max(1, int(max_candidates or (max_results * 2)))
    if len(filtered) > max_with_neighbors:
        filtered = filtered[:max_with_neighbors]

    return filtered


# ── Busca semantica (hibrida: vetor + full-text) ─────────
def _section_ids_from_hits(section_hits: list[dict], limit: int | None = None) -> list[str]:
    seen: set[str] = set()
    section_ids: list[str] = []
    for section in section_hits:
        section_id = str(section.get("id") or "").strip()
        if not section_id or section_id in seen:
            continue
        seen.add(section_id)
        section_ids.append(section_id)
        if limit is not None and len(section_ids) >= limit:
            break
    return section_ids


def _limit_chunk_diversity(
    chunks: list[dict],
    *,
    max_per_section: int | None = None,
    max_per_document: int | None = None,
    top_limit: int | None = None,
) -> list[dict]:
    if not chunks:
        return []

    section_cap = max(1, int(max_per_section or config.MAX_CHUNKS_PER_SECTION))
    document_cap = max(section_cap, int(max_per_document or config.MAX_CHUNKS_PER_DOCUMENT))
    diversified: list[dict] = []
    section_counts: dict[str, int] = {}
    document_counts: dict[str, int] = {}

    for chunk in chunks:
        document_key = str(chunk.get("document_id") or chunk.get("filename") or "desconhecido")
        section_value = chunk.get("section_id")
        section_key = str(section_value).strip() if section_value else ""

        if section_key and section_counts.get(section_key, 0) >= section_cap:
            continue
        if document_counts.get(document_key, 0) >= document_cap:
            continue

        diversified.append(chunk)
        document_counts[document_key] = document_counts.get(document_key, 0) + 1
        if section_key:
            section_counts[section_key] = section_counts.get(section_key, 0) + 1

        if top_limit is not None and len(diversified) >= top_limit:
            break

    return diversified


def _should_rerank_chunks(chunks: list[dict]) -> bool:
    if not config.RAG_ENABLE_RERANKING:
        return False
    if not chunks or len(chunks) <= 2:
        return False

    top_sim = _safe_similarity(chunks[0].get("similarity", 0))
    if top_sim < config.RERANKER_MIN_TRIGGER_SIM:
        return False
    if top_sim > config.RERANKER_MAX_TRIGGER_SIM:
        return False

    distinct_docs = {
        str(chunk.get("document_id") or chunk.get("filename") or "")
        for chunk in chunks
        if chunk.get("document_id") or chunk.get("filename")
    }
    distinct_sections = {
        str(chunk.get("section_id"))
        for chunk in chunks
        if chunk.get("section_id")
    }
    return len(distinct_docs) > 1 or len(distinct_sections) > 1 or len(chunks) >= 4


def search_relevant_sections(
    query: str,
    max_results: int = None,
    threshold: float = None,
    query_plan: dict | None = None,
) -> list[dict]:
    if not config.SECTION_RETRIEVAL_ENABLED:
        return []
    if max_results is None:
        max_results = config.SECTION_MATCH_COUNT
    if threshold is None:
        threshold = config.SIMILARITY_THRESHOLD

    query_for_embedding, query_for_fts = _preprocess_query(query)
    query_embedding = _get_cached_query_embedding(query_for_embedding)
    doc_types_filter, module_filter = _build_search_filters(query_plan)
    rpc_params: dict[str, Any] = {
        "query_embedding": embedding_to_pgvector(query_embedding),
        "query_text": query_for_fts,
        "match_count": max_results,
        "match_threshold": threshold,
        "fetch_limit": max(max_results, int(config.SECTION_FETCH_LIMIT)),
    }
    if doc_types_filter:
        rpc_params["filter_doc_types"] = doc_types_filter
    if module_filter:
        rpc_params["filter_modules"] = module_filter

    try:
        result = _search_rpc_with_filter_fallback("hybrid_match_sections", rpc_params)
    except Exception as e:
        if _is_missing_rpc_function(e, "hybrid_match_sections"):
            logger.warning(
                "RPC hybrid_match_sections nao encontrada; seguindo com retrieval direto por chunks."
            )
            return []
        logger.warning("Busca por secoes falhou: %s", e)
        return []

    if not result:
        return []

    processed = _postprocess_search_results(
        result,
        max_results,
        threshold,
        max_candidates=max(max_results, int(config.SECTION_FETCH_LIMIT)),
        ranking_mode="retrieval",
    )
    logger.info(
        "Busca por secoes: %d secoes candidatas para '%s'",
        len(processed),
        query[:80],
    )
    return processed


def search_similar_chunks(
    query: str,
    max_results: int = None,
    threshold: float = None,
    query_plan: dict | None = None,
    section_ids: list[str] | None = None,
    candidate_limit: int | None = None,
) -> list[dict]:
    if max_results is None:
        max_results = config.MAX_CONTEXT_CHUNKS
    if threshold is None:
        threshold = config.SIMILARITY_THRESHOLD
    fetch_limit = max(max_results, int(candidate_limit or config.CHUNK_FETCH_LIMIT))

    query_for_embedding, query_for_fts = _preprocess_query(query)
    query_embedding = _get_cached_query_embedding(query_for_embedding)
    doc_types_filter, module_filter = _build_search_filters(query_plan)
    rpc_params = {
        "query_embedding": embedding_to_pgvector(query_embedding),
        "query_text": query_for_fts,
        "match_count": max_results,
        "match_threshold": threshold,
        "fetch_limit": fetch_limit,
    }
    if doc_types_filter:
        rpc_params["filter_doc_types"] = doc_types_filter
    if module_filter:
        rpc_params["filter_modules"] = module_filter
    if section_ids:
        rpc_params["filter_section_ids"] = section_ids

    if query_plan and (doc_types_filter or module_filter or section_ids):
        logger.info(
            "Roteamento de busca: intent=%s doc_types=%s modules=%s sections=%d",
            query_plan.get("intent", "general"),
            doc_types_filter or [],
            module_filter or [],
            len(section_ids or []),
        )

    # Tentar busca hibrida primeiro (vetor + full-text com RRF)
    try:
        result = _search_rpc_with_filter_fallback(
            "hybrid_match_chunks",
            rpc_params,
        )
        if result:
            result = _postprocess_search_results(
                result,
                max_results,
                threshold,
                max_candidates=fetch_limit,
                ranking_mode="retrieval",
            )
        if result:
            logger.info(
                "Busca hibrida: %d chunks encontrados para '%s'",
                len(result),
                query[:80],
            )
            return result
        logger.info(
            "Busca hibrida sem chunks acima do piso minimo para '%s'",
            query[:80],
        )
    except Exception as e:
        logger.warning(
            "Busca hibrida falhou (%s), usando busca vetorial pura.", e
        )

    # Fallback: busca vetorial pura (match_chunks original)
    vector_params = {
        "query_embedding": rpc_params["query_embedding"],
        "match_count": max_results,
        "match_threshold": threshold,
        "fetch_limit": fetch_limit,
    }
    if doc_types_filter:
        vector_params["filter_doc_types"] = doc_types_filter
    if module_filter:
        vector_params["filter_modules"] = module_filter
    if section_ids:
        vector_params["filter_section_ids"] = section_ids

    result = _search_rpc_with_filter_fallback(
        "match_chunks",
        vector_params,
    )
    if result:
        result = _postprocess_search_results(
            result,
            max_results,
            threshold,
            max_candidates=fetch_limit,
            ranking_mode="retrieval",
        )

    if result:
        logger.info(
            "Busca vetorial pura: %d chunks encontrados para '%s'",
            len(result),
            query[:80],
        )
    else:
        logger.warning(
            "Nenhum chunk encontrado para '%s' (threshold=%.2f)",
            query[:80],
            threshold,
        )

    final_result = result or []

    # P0.3: Se a busca filtrada retornou poucos resultados, complementar sem filtro de modulo
    if module_filter and not section_ids and len(final_result) < max(1, max_results // 3):
        logger.info(
            "Busca filtrada retornou poucos resultados (%d/%d); "
            "complementando com busca sem filtro de modulo.",
            len(final_result),
            max_results,
        )
        found_ids = {c.get("id") for c in final_result}
        unfiltered_params = {
            "query_embedding": rpc_params["query_embedding"],
            "query_text": query_for_fts,
            "match_count": max_results,
            "match_threshold": threshold,
            "fetch_limit": fetch_limit,
        }
        if doc_types_filter:
            unfiltered_params["filter_doc_types"] = doc_types_filter
        try:
            extra = _search_rpc_with_filter_fallback("hybrid_match_chunks", unfiltered_params)
            if extra:
                extra = _postprocess_search_results(
                    extra,
                    max_results,
                    threshold,
                    max_candidates=fetch_limit,
                    ranking_mode="retrieval",
                )
                for chunk in extra:
                    if chunk.get("id") not in found_ids:
                        final_result.append(chunk)
                        found_ids.add(chunk.get("id"))
                max_with_neighbors = max(1, fetch_limit)
                if len(final_result) > max_with_neighbors:
                    final_result = final_result[:max_with_neighbors]
                logger.info(
                    "Busca complementar sem filtro de modulo: %d chunks total apos merge.",
                    len(final_result),
                )
        except Exception as e:
            logger.warning("Busca complementar sem filtro de modulo falhou: %s", e)

    return final_result


def _normalize_scope(scope: dict | None) -> dict[str, str]:
    if not isinstance(scope, dict):
        return {}
    normalized: dict[str, str] = {}
    for key in ("level", "tenant", "erp", "version"):
        value = scope.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            normalized[key] = text
    return normalized


def _search_feedback_memory_chunks(
    query: str,
    *,
    scope: dict | None = None,
    scope_level: str | None = None,
    max_results: int | None = None,
    threshold: float | None = None,
) -> list[dict]:
    if max_results is None:
        max_results = config.RAG_FEEDBACK_TOP_K
    if threshold is None:
        threshold = config.RAG_FEEDBACK_MIN_SIMILARITY

    query_for_embedding, _ = _preprocess_query(query)
    query_embedding = _get_cached_query_embedding(query_for_embedding)
    normalized_scope = _normalize_scope(scope)

    rpc_params: dict[str, Any] = {
        "query_embedding": embedding_to_pgvector(query_embedding),
        "match_count": max_results,
        "match_threshold": threshold,
    }
    if scope_level:
        rpc_params["scope_level"] = scope_level
    if normalized_scope.get("tenant"):
        rpc_params["scope_tenant"] = normalized_scope["tenant"]
    if normalized_scope.get("erp"):
        rpc_params["scope_erp"] = normalized_scope["erp"]
    if normalized_scope.get("version"):
        rpc_params["scope_version"] = normalized_scope["version"]

    try:
        rows = supabase_rpc("search_feedback_chunks", rpc_params)
    except Exception as e:
        if _is_missing_rpc_function(e, "search_feedback_chunks"):
            logger.warning(
                "RPC search_feedback_chunks nao encontrada; memoria de feedback desativada."
            )
            return []
        logger.warning("Erro ao buscar feedback chunks: %s", e)
        return []

    source_kind = "feedback_global" if scope_level == "global" else "feedback_scoped"
    priority = 40 if source_kind == "feedback_scoped" else 32
    feedback_priority = 2 if source_kind == "feedback_scoped" else 1
    formatted: list[dict] = []
    for row in rows or []:
        feedback_item_id = str(row.get("feedback_item_id") or "")
        vector_similarity = _safe_similarity(row.get("similarity", 0.0))
        formatted.append(
            {
                "id": row.get("id"),
                "document_id": f"feedback:{feedback_item_id or row.get('id')}",
                "content": row.get("content") or "",
                "chunk_index": 0,
                "filename": f"feedback_{feedback_item_id or row.get('id')}.md",
                "similarity": vector_similarity,
                "vector_similarity": vector_similarity,
                "lexical_score": None,
                "fusion_score": None,
                "feedback_priority": feedback_priority,
                "retrieval_origin": source_kind,
                "is_neighbor": False,
                "metadata": {
                    "doc_priority": priority,
                    "source_kind": source_kind,
                    "feedback_item_id": feedback_item_id,
                    "scope": row.get("scope") if isinstance(row.get("scope"), dict) else {},
                },
            }
        )
    return formatted


def _dedupe_chunks(chunks: list[dict]) -> list[dict]:
    deduped: list[dict] = []
    seen: set[str] = set()
    for chunk in chunks:
        chunk_id = chunk.get("id")
        key = str(chunk_id) if chunk_id is not None else (
            f"{chunk.get('filename','')}::{_chunk_index_value(chunk)}::{hash(chunk.get('content',''))}"
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(chunk)
    return deduped


def retrieve_chunks_with_feedback(
    query: str,
    *,
    query_plan: dict | None,
    scope: dict | None,
) -> tuple[list[dict], list[dict], list[dict]]:
    scope = _normalize_scope(scope)
    scoped_feedback = []
    if scope:
        scoped_feedback = _search_feedback_memory_chunks(
            query,
            scope=scope,
            scope_level=scope.get("level") or "tenant",
        )

    global_feedback = _search_feedback_memory_chunks(
        query,
        scope={},
        scope_level="global",
    )

    section_hits = search_relevant_sections(
        query,
        query_plan=query_plan,
        max_results=config.SECTION_MATCH_COUNT,
    )
    section_scope_ids = _section_ids_from_hits(section_hits, limit=config.SECTION_MATCH_COUNT)
    kb_chunks: list[dict] = []

    if section_scope_ids:
        kb_chunks = search_similar_chunks(
            query,
            query_plan=query_plan,
            section_ids=section_scope_ids,
            candidate_limit=config.CHUNK_FETCH_LIMIT,
        )

    best_section_similarity = max(
        (_safe_similarity(section.get("similarity", 0.0)) for section in section_hits),
        default=0.0,
    )
    if not kb_chunks or best_section_similarity < (config.SIMILARITY_THRESHOLD * config.SIMILARITY_FLOOR_FACTOR):
        fallback_chunks = search_similar_chunks(
            query,
            query_plan=query_plan,
            candidate_limit=config.CHUNK_FETCH_LIMIT,
        )
        kb_chunks = _dedupe_chunks(kb_chunks + fallback_chunks)

    merged = _dedupe_chunks(scoped_feedback + global_feedback + kb_chunks)
    return merged, scoped_feedback, kb_chunks


def _chunk_index_value(chunk: dict) -> int:
    try:
        return int(chunk.get("chunk_index", 0))
    except (TypeError, ValueError):
        return 0


def _trim_chunk_overlap(previous_text: str, current_text: str, max_overlap: int) -> str:
    if not previous_text or not current_text or max_overlap <= 0:
        return current_text

    max_window = min(max_overlap, len(previous_text), len(current_text))
    min_overlap = 40
    if max_window < min_overlap:
        return current_text
    for overlap_size in range(max_window, min_overlap - 1, -1):
        if previous_text[-overlap_size:] == current_text[:overlap_size]:
            return current_text[overlap_size:].lstrip()
    return current_text


def _merge_document_chunks(doc_chunks: list[dict]) -> str:
    parts: list[str] = []
    previous_content = ""
    previous_chunk_index: int | None = None

    for chunk in doc_chunks:
        content = chunk.get("content", "") or ""
        if not content.strip():
            continue

        chunk_index = _chunk_index_value(chunk)
        merged_content = content
        if previous_chunk_index is not None and chunk_index == previous_chunk_index + 1:
            merged_content = _trim_chunk_overlap(
                previous_content,
                content,
                config.CHUNK_OVERLAP,
            )

        if merged_content.strip():
            parts.append(merged_content)

        previous_content = content
        previous_chunk_index = chunk_index

    return "\n\n".join(parts)


def _chunk_meta(chunk: dict) -> dict:
    meta = chunk.get("metadata") or {}
    return meta if isinstance(meta, dict) else {}


def _chunk_analytical_value(chunk: dict, key: str, default=None):
    value = chunk.get(key)
    if value not in (None, "", {}, []):
        return value
    return _chunk_meta(chunk).get(key, default)


def _merge_chunk_entities(doc_chunks: list[dict]) -> dict[str, list[str]]:
    merged: dict[str, list[str]] = {}
    seen: dict[str, set[str]] = {}

    for chunk in doc_chunks:
        entities = _chunk_analytical_value(chunk, "entities", {})
        if not isinstance(entities, dict):
            continue
        for key, values in entities.items():
            if not isinstance(values, list):
                continue
            bucket = merged.setdefault(str(key), [])
            bucket_seen = seen.setdefault(str(key), set())
            for value in values:
                clean = str(value or "").strip()
                if not clean:
                    continue
                normalized = normalize_text(clean)
                if normalized in bucket_seen:
                    continue
                bucket_seen.add(normalized)
                bucket.append(clean)
                if len(bucket) >= 12:
                    break

    return {key: values for key, values in merged.items() if values}


def _format_entities_for_context(entities: dict[str, list[str]]) -> str:
    labels = {
        "tables": "Tabelas",
        "parameters": "Parametros",
        "endpoints": "Endpoints",
        "statuses": "Status/codigos",
        "error_terms": "Termos de erro",
    }
    parts = []
    for key, label in labels.items():
        values = entities.get(key) or []
        if values:
            parts.append(f"{label}: {', '.join(values[:10])}")
    return "\n".join(parts)


def _build_analytical_context_block(doc_chunks: list[dict]) -> str:
    heading_paths: list[str] = []
    semantic_contexts: list[str] = []
    answer_modes: list[str] = []
    for chunk in doc_chunks:
        heading_path = str(_chunk_analytical_value(chunk, "heading_path", "") or "").strip()
        if heading_path and heading_path not in heading_paths:
            heading_paths.append(heading_path)

        semantic_context = str(_chunk_analytical_value(chunk, "semantic_context", "") or "").strip()
        if semantic_context and semantic_context not in semantic_contexts:
            semantic_contexts.append(semantic_context)

        answer_mode = str(_chunk_analytical_value(chunk, "answer_mode", "") or "").strip()
        if answer_mode and answer_mode != "general" and answer_mode not in answer_modes:
            answer_modes.append(answer_mode)

    entities_text = _format_entities_for_context(_merge_chunk_entities(doc_chunks))
    lines: list[str] = []
    if heading_paths:
        lines.append(f"Assunto/secoes: {' | '.join(heading_paths[:4])}")
    if answer_modes:
        lines.append(f"Tipo de analise sugerida: {', '.join(answer_modes[:4])}")
    if semantic_contexts:
        lines.append("Resumo operacional recuperado:")
        lines.extend(f"- {ctx}" for ctx in semantic_contexts[:4])
    if entities_text:
        lines.append("Entidades extraidas:")
        lines.append(entities_text)

    if not lines:
        return ""

    return (
        "<analytical_context>\n"
        + "\n".join(lines)
        + "\n</analytical_context>"
    )


# -- Montagem do contexto -------------------------------------------------------
def build_context(chunks: list[dict]) -> str:
    if not chunks:
        return ""

    chunks = _limit_chunk_diversity(
        chunks,
        max_per_section=config.MAX_CHUNKS_PER_SECTION,
        max_per_document=config.MAX_CHUNKS_PER_DOCUMENT,
    )
    if not chunks:
        return ""

    chunks_by_doc: dict[str, list[dict]] = {}
    for chunk in chunks:
        doc_key = str(chunk.get("document_id") or chunk.get("filename") or "desconhecido")
        chunks_by_doc.setdefault(doc_key, []).append(chunk)

    docs_for_context: list[dict] = []
    for doc_chunks in chunks_by_doc.values():
        sorted_doc_chunks = sorted(doc_chunks, key=_chunk_index_value)
        merged_content = _merge_document_chunks(sorted_doc_chunks)
        if not merged_content.strip():
            continue
        analytical_context = _build_analytical_context_block(sorted_doc_chunks)

        filename = next(
            (c.get("filename") for c in sorted_doc_chunks if c.get("filename")),
            "desconhecido",
        )
        max_similarity = max(
            _safe_similarity(
                chunk.get("vector_similarity", chunk.get("similarity", 0.0))
            )
            for chunk in sorted_doc_chunks
        )

        docs_for_context.append(
            {
                "filename": filename,
                "max_similarity": max_similarity,
                "merged_content": merged_content,
                "analytical_context": analytical_context,
                "chunk_count": len(sorted_doc_chunks),
            }
        )

    context_parts = []
    for index, doc in enumerate(docs_for_context, start=1):
        doc_body = doc["merged_content"]
        if doc.get("analytical_context"):
            doc_body = f"{doc['analytical_context']}\n\n<evidence>\n{doc_body}\n</evidence>"
        context_parts.append(
            f"<document index=\"{index}\" source=\"{doc['filename']}\" relevance=\"{doc['max_similarity']:.2f}\" chunks=\"{doc['chunk_count']}\">\n"
            f"{doc_body}\n"
            f"</document>"
        )

    return "\n\n".join(context_parts)


# -- Reformulacao de query com historico (P0.1) ---------------------------------
def _reformulate_query_with_history(
    question: str,
    conversation_history: list[dict] | None,
) -> str:
    """Usa historico recente para tornar perguntas de follow-up autocontidas."""
    if not config.RAG_ENABLE_QUERY_REFORMULATION:
        return question
    if not conversation_history:
        return question

    # Perguntas com 5+ palavras provavelmente ja sao autocontidas
    if len(question.split()) >= 5:
        return question

    # Usar apenas os ultimos 2 pares (4 mensagens)
    recent = conversation_history[-4:]
    history_text = "\n".join(
        f"{'Usuario' if m['role'] == 'user' else 'Assistente'}: "
        f"{m['content'][:300] if isinstance(m['content'], str) else '[imagem]'}"
        for m in recent
    )

    try:
        response = _gemini_generate(
            model=config.REFORMULATION_MODEL,
            max_tokens=200,
            system=(
                "Voce reescreve perguntas de follow-up para serem autocontidas. "
                "Substitua pronomes e referencias vagas pelo tema correto do historico. "
                "Retorne APENAS a pergunta reescrita, sem explicacao. "
                "Se a pergunta ja for autocontida, retorne-a como esta."
            ),
            contents=(
                f"Historico recente:\n{history_text}\n\n"
                f"Pergunta atual: {question}\n\n"
                "Reescreva a pergunta para ser autocontida:"
            ),
        )
        reformulated = response.text.strip()
        if reformulated and len(reformulated) < 500:
            if _looks_like_clarifying_request(reformulated):
                logger.info(
                    "Query reformulada descartada por virar pedido de esclarecimento: '%s'",
                    reformulated[:80],
                )
                return question
            logger.info(
                "Query reformulada: '%s' -> '%s'",
                question[:60],
                reformulated[:60],
            )
            return reformulated
    except Exception as e:
        logger.warning("Erro na reformulacao de query: %s", e)

    return question


# -- Re-ranking com LLM (P1.1) -------------------------------------------------
def _rerank_chunks_with_llm(
    query: str,
    chunks: list[dict],
    top_n: int | None = None,
) -> list[dict]:
    """Re-ranking seletivo para zona cinzenta, com diversidade antes do LLM."""
    if not chunks or len(chunks) <= 2:
        return chunks
    if not _should_rerank_chunks(chunks):
        logger.info(
            "Re-ranking LLM pulado: fora da zona cinzenta configurada (top_similarity=%.3f).",
            _safe_similarity(chunks[0].get("similarity", 0)),
        )
        return chunks

    if top_n is None:
        top_n = config.MAX_CONTEXT_CHUNKS

    diversified_candidates = _limit_chunk_diversity(
        chunks,
        max_per_section=max(1, config.MAX_CHUNKS_PER_SECTION),
        max_per_document=max(2, config.MAX_CHUNKS_PER_DOCUMENT),
        top_limit=max(int(config.RERANKER_MAX_CANDIDATES), top_n * 2),
    )
    candidate_count = max(2, min(int(config.RERANKER_MAX_CANDIDATES), len(diversified_candidates)))
    candidates = diversified_candidates[:candidate_count]
    chunk_summaries: list[str] = []
    for i, chunk in enumerate(candidates):
        content = (chunk.get("content") or "")[:500].rsplit(" ", 1)[0]
        filename = chunk.get("filename", "")
        heading_path = str(_chunk_analytical_value(chunk, "heading_path", "") or "").strip()
        answer_mode = str(_chunk_analytical_value(chunk, "answer_mode", "") or "").strip()
        entities = _format_entities_for_context(_merge_chunk_entities([chunk]))
        meta_parts = [filename]
        if heading_path:
            meta_parts.append(heading_path)
        if answer_mode and answer_mode != "general":
            meta_parts.append(f"modo={answer_mode}")
        if entities:
            meta_parts.append(entities.replace("\n", " | "))
        header = " | ".join(part for part in meta_parts if part)
        chunk_summaries.append(f"[{i}] ({header}) {content}")

    summaries_text = "\n---\n".join(chunk_summaries)

    try:
        response = _gemini_generate(
            model=config.RERANKER_MODEL,
            max_tokens=200,
            system=(
                "Voce e um ranqueador de documentacao tecnica do ERP maxPedido (Maxima Sistemas). "
                "Dada uma pergunta de suporte N1 e trechos da base de conhecimento, retorne os indices "
                "dos trechos mais relevantes para responder a pergunta, do mais relevante ao menos. "
                "Prefira trechos com passos especificos, consultas SQL completas, nomes de "
                "campo/tela/parametro e resolucoes de erro em vez de trechos introdutorios ou genericos. "
                "Retorne APENAS os numeros separados por virgula. Exemplo: 3,0,7,1"
            ),
            contents=f"Pergunta: {query}\n\nTrechos:\n{summaries_text}",
        )

        ranking_text = response.text.strip()
        indices: list[int] = []
        for part in ranking_text.replace(" ", "").split(","):
            try:
                idx = int(part)
                if 0 <= idx < len(candidates) and idx not in indices:
                    indices.append(idx)
            except ValueError:
                continue

        if indices:
            reranked = [candidates[i] for i in indices]
            seen = set(indices)
            for i, chunk in enumerate(candidates):
                if i not in seen:
                    reranked.append(chunk)
            reranked_ids = {chunk.get("id") for chunk in reranked if chunk.get("id") is not None}
            for chunk in chunks:
                chunk_id = chunk.get("id")
                if chunk_id is not None and chunk_id in reranked_ids:
                    continue
                reranked.append(chunk)
            logger.info("Re-ranking LLM aplicado: %d chunks reordenados", len(indices))
            return reranked

    except Exception as e:
        logger.warning("Erro no re-ranking LLM: %s", e)

    return chunks


# -- Resposta do modelo ---------------------------------------------------------
def _compose_gemini_contents(
    question: str,
    conversation_history: list[dict] | None,
    images: list[dict] | None,
) -> list[_gtypes.Content]:
    user_parts: list[_gtypes.Part] = []
    if images:
        for img in images:
            user_parts.append(_gtypes.Part(
                inline_data=_gtypes.Blob(
                    mime_type=img["media_type"],
                    data=_base64.b64decode(img["data"]),
                )
            ))
    user_parts.append(_gtypes.Part(text=question))

    gemini_contents: list[_gtypes.Content] = []
    if conversation_history:
        gemini_contents = _anthropic_msgs_to_gemini(conversation_history)
    gemini_contents.append(_gtypes.Content(role="user", parts=user_parts))
    return gemini_contents


def _to_openai_content(content) -> str | list[dict]:
    if isinstance(content, str):
        return content

    parts: list[dict] = []
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                text = block.strip()
                if text:
                    parts.append({"type": "text", "text": text})
                continue
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "").lower()
            if block_type == "text" and block.get("text"):
                parts.append({"type": "text", "text": str(block["text"])})
            elif block_type == "image":
                src = block.get("source") or {}
                media_type = str(src.get("media_type") or "image/png")
                data = str(src.get("data") or "")
                if data:
                    parts.append(
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{media_type};base64,{data}"},
                        }
                    )

    if not parts:
        return str(content)
    if len(parts) == 1 and parts[0]["type"] == "text":
        return str(parts[0]["text"])
    return parts


def _compose_openai_messages(
    *,
    question: str,
    system: str,
    conversation_history: list[dict] | None,
    images: list[dict] | None,
) -> list[dict]:
    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})

    for msg in conversation_history or []:
        role_raw = str(msg.get("role") or "user").lower()
        role = "assistant" if role_raw == "assistant" else "user"
        messages.append({"role": role, "content": _to_openai_content(msg.get("content", ""))})

    user_parts: list[dict] = [{"type": "text", "text": question}]
    for img in images or []:
        media_type = str(img.get("media_type") or "image/png")
        data = str(img.get("data") or "")
        if not data:
            continue
        user_parts.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:{media_type};base64,{data}"},
            }
        )
    user_content: str | list[dict]
    if len(user_parts) == 1:
        user_content = user_parts[0]["text"]
    else:
        user_content = user_parts
    messages.append({"role": "user", "content": user_content})
    return messages


def _ask_model(
    *,
    question: str,
    system: str,
    conversation_history: list[dict] | None,
    images: list[dict] | None,
    max_tokens_override: int | None = None,
) -> str:
    provider = _active_llm_provider()
    requested_max_tokens = int(max_tokens_override or config.ASK_MAX_TOKENS)
    try:
        if provider == "openai":
            max_tokens = max(256, min(requested_max_tokens, int(config.OPENAI_MAX_OUTPUT_TOKENS)))
            prompt_chars = len(question or "") + len(system or "")
            for msg in conversation_history or []:
                prompt_chars += len(str(msg.get("content", "")))
            messages = _compose_openai_messages(
                question=question,
                system=system,
                conversation_history=conversation_history,
                images=images,
            )
            primary_model = _resolve_text_model(config.GENERATION_MODEL, purpose="general")
            if prompt_chars > 12000:
                primary_model = _resolve_text_model(config.OPENAI_CONTEXTUAL_MODEL, purpose="contextual")
            response = _openai_chat_generate(
                model=primary_model,
                messages=messages,
                max_tokens=max_tokens,
            )
            if response.text:
                return response.text

            fallback_model = _resolve_text_model(config.OPENAI_CONTEXTUAL_MODEL, purpose="contextual")
            if fallback_model != primary_model:
                logger.warning(
                    "OpenAI retornou resposta vazia no modelo %s; tentando fallback %s.",
                    primary_model,
                    fallback_model,
                )
                fallback_response = _openai_chat_generate(
                    model=fallback_model,
                    messages=messages,
                    max_tokens=max_tokens,
                )
                if fallback_response.text:
                    return fallback_response.text

            logger.warning("Resposta inesperada do OpenAI Chat Completions (texto vazio apos fallback).")
            return _provider_error_response("Nao foi possivel extrair uma resposta do modelo.")

        max_tokens = max(128, requested_max_tokens)
        gemini_contents = _compose_gemini_contents(question, conversation_history, images)
        response = _gemini_generate(
            model=config.GENERATION_MODEL,
            max_tokens=max_tokens,
            system=system,
            contents=gemini_contents,
        )
        if response.text:
            return response.text
        logger.warning("Resposta inesperada do Gemini: %s", response)
        return _provider_error_response("Nao foi possivel extrair uma resposta do modelo.")
    except Exception as e:
        error_str = str(e).lower()
        provider_label = "OpenAI" if provider == "openai" else "Gemini"
        if "429" in str(e) or "resource_exhausted" in error_str or "rate" in error_str:
            logger.error("Rate limit do %s atingido: %s", provider_label, e)
            return _provider_error_response(
                "O servico esta sobrecarregado no momento. Tente novamente em alguns segundos."
            )
        if "401" in str(e) or "403" in str(e) or "api_key" in error_str or "permission" in error_str:
            logger.error("Erro de autenticacao com %s: %s", provider_label, e)
            return _provider_error_response("Erro de configuracao do bot. Contate o administrador.")
        if "timeout" in error_str:
            logger.error("Timeout na chamada ao %s: %s", provider_label, e)
            return _provider_error_response(
                "A consulta demorou demais. Tente reformular com uma pergunta mais curta."
            )
        if "connect" in error_str:
            logger.error("Erro de conexao com %s: %s", provider_label, e)
            return _provider_error_response(
                "Nao foi possivel conectar ao servico. Tente novamente em instantes."
            )
        logger.error("Erro ao chamar %s: %s", provider_label, e, exc_info=True)
        return _provider_error_response("Ocorreu um erro inesperado. Tente novamente.")


def _should_strict_abstain(question: str, chunks: list[dict]) -> tuple[bool, str | None]:
    if not config.RAG_STRICT_ABSTAIN:
        return False, None
    if not chunks:
        return True, "no_chunks"

    top_similarity = max(
        (
            _safe_similarity(
                chunk.get("vector_similarity", chunk.get("similarity", 0.0))
            )
            for chunk in chunks
        ),
        default=0.0,
    )
    if len(chunks) < config.RAG_MIN_RETRIEVED_CHUNKS:
        return True, "few_chunks"
    if top_similarity < config.RAG_MIN_STRONG_SIMILARITY:
        return True, "low_similarity"
    if _is_operational_specific_query(question):
        required = min(1.0, config.RAG_MIN_STRONG_SIMILARITY + config.RAG_OPERATIONAL_SIMILARITY_MARGIN)
        if top_similarity < required:
            return True, "low_similarity_operational"
    return False, None


def _apply_grounding_regeneration(
    *,
    answer: str,
    question: str,
    system: str,
    conversation_history: list[dict] | None,
    images: list[dict] | None,
    allowed_sources: set[str],
    source_display_map: dict[str, str] | None = None,
) -> tuple[str, list[str], set[str], int]:
    if _is_provider_error_response(answer):
        return str(answer).strip(), [], set(), 0

    if not config.RAG_ENABLE_GROUNDING_VALIDATION:
        normalized_answer, cited = _enforce_sources_section_only(
            answer,
            allowed_sources=allowed_sources,
            source_display_map=source_display_map,
        )
        return normalized_answer, [], cited, 0

    valid, errors, cited_sources = _validate_grounded_answer(
        answer=answer,
        allowed_sources=allowed_sources,
        question=question,
        require_sources_section=config.RAG_REQUIRE_SOURCES_SECTION,
    )
    if valid:
        normalized_answer, cited_sources = _enforce_sources_section_only(
            answer,
            allowed_sources=allowed_sources,
            source_display_map=source_display_map,
        )
        return normalized_answer, [], cited_sources, 0
    if not _is_grounding_error_critical(errors):
        normalized_answer, cited_sources = _enforce_sources_section_only(
            answer,
            allowed_sources=allowed_sources,
            source_display_map=source_display_map,
        )
        logger.info(
            "Grounding inicial com erros nao-criticos; mantendo resposta sem regeneracao: %s",
            " | ".join(errors),
        )
        return normalized_answer, errors, cited_sources, 0

    max_regen_attempts = max(0, int(config.RAG_MAX_REGEN_ATTEMPTS))
    regeneration_attempts = 0
    revised_answer = (answer or "").strip()
    revised_errors = errors
    revised_citations = cited_sources
    for _ in range(max_regen_attempts):
        regeneration_attempts += 1
        revision_prompt = (
            f"{question}\n\n"
            "Sua resposta anterior falhou na validacao de grounding.\n"
            f"Erros detectados: {' | '.join(errors)}\n"
            f"Fontes permitidas: {', '.join(sorted(allowed_sources)) or '(nenhuma)'}\n\n"
            "Reescreva seguindo estritamente:\n"
            "1) Use somente informacoes sustentadas pelo contexto.\n"
            "2) Nao inclua [fonte: ...] no meio dos paragrafos.\n"
            "3) Inclua secao final obrigatoria 'Fontes:' listando apenas arquivos usados.\n"
        )
        revised_answer = _ask_model(
            question=revision_prompt,
            system=system,
            conversation_history=None,
            images=None,
            max_tokens_override=1024,
        )
        if _is_provider_error_response(revised_answer):
            return str(revised_answer).strip(), [], set(), regeneration_attempts

        valid, revised_errors, revised_citations = _validate_grounded_answer(
            answer=revised_answer,
            allowed_sources=allowed_sources,
            question=question,
            require_sources_section=config.RAG_REQUIRE_SOURCES_SECTION,
        )
        if valid:
            revised_answer, revised_citations = _enforce_sources_section_only(
                revised_answer,
                allowed_sources=allowed_sources,
                source_display_map=source_display_map,
            )
            return revised_answer, [], revised_citations, regeneration_attempts

    if _is_grounding_error_critical(revised_errors):
        return _build_abstain_response(question), revised_errors, set(), regeneration_attempts

    logger.info(
        "Grounding com erros nao-criticos; mantendo resposta sem abstencao: %s",
        " | ".join(revised_errors),
    )
    best_answer = (revised_answer or "").strip() or (answer or "").strip()
    best_answer, revised_citations = _enforce_sources_section_only(
        best_answer,
        allowed_sources=allowed_sources,
        source_display_map=source_display_map,
    )
    return best_answer, revised_errors, revised_citations, regeneration_attempts


def ask(
    question: str,
    conversation_history: list[dict] = None,
    images: list[dict] = None,
    system_prompt: str = None,
    platform: str = "unknown",
    scope: dict | None = None,
) -> tuple[str, list[dict], dict]:
    """
    Responde uma pergunta usando RAG + Gemini e retorna trace de telemetria.

    Retorno: (answer, retrieved_chunks, trace)
    """
    t0 = _time.monotonic()
    query_id = _new_query_id()
    trace: dict[str, Any] = {
        "query_id": query_id,
        "platform": platform,
        "abstained": False,
        "abstention_reason": None,
        "confidence": 0.0,
        "query_plan": {},
        "retrieved_sources": [],
        "retrieved_chunk_count": 0,
        "top_similarity": 0.0,
        "top_vector_similarity": 0.0,
        "top_lexical_score": 0.0,
        "top_fusion_score": 0.0,
        "top_feedback_priority": 0.0,
        "retrieval_origins": [],
        "citations": [],
        "cited_files": [],
        "grounding_errors": [],
        "regeneration_attempts": 0,
        "response_state": None,
        "citation_validation": {
            "syntax": "not_evaluated",
            "semantic_support": "not_evaluated",
        },
        "stage_timings_ms": {},
    }

    def _mark_stage(stage_name: str, started_at: float) -> None:
        trace["stage_timings_ms"][stage_name] = int((_time.monotonic() - started_at) * 1000)

    stage_started_at = _time.monotonic()
    search_query = _reformulate_query_with_history(question, conversation_history)
    _mark_stage("reformulation", stage_started_at)
    base_system = system_prompt or config.SYSTEM_PROMPT

    if config.FULL_CONTEXT_ENABLED:
        full_context = _load_full_context_docs()
        chunks: list[dict] = []
        system = base_system
        if full_context:
            system += (
                "\n\n<knowledge_base>\n"
                "Abaixo esta a BASE DE CONHECIMENTO COMPLETA da Maxima Sistemas. "
                "Use apenas informacoes explicitamente presentes nesses documentos.\n\n"
                f"{full_context}\n"
                "</knowledge_base>"
            )
        else:
            answer = "Base de conhecimento indisponivel no momento. Tente novamente."
            trace["abstained"] = True
            trace["abstention_reason"] = "full_context_unavailable"
            _set_response_state(
                trace,
                "insufficient_evidence",
                citation_syntax="not_applicable",
                semantic_support="insufficient_evidence",
            )
            trace["latency_ms"] = int((_time.monotonic() - t0) * 1000)
            _log_ask_trace(trace)
            return answer, chunks, trace
        stage_started_at = _time.monotonic()
        answer = _ask_model(
            question=question,
            system=system,
            conversation_history=conversation_history,
            images=images,
        )
        _mark_stage("generation", stage_started_at)
        if _is_provider_error_response(answer):
            _set_response_state(
                trace,
                "provider_error",
                citation_syntax="not_applicable",
                semantic_support="not_evaluated",
            )
        elif answer.startswith(config.NO_ANSWER_PHRASE):
            trace["abstained"] = True
            trace["abstention_reason"] = "model_insufficient_evidence"
            _set_response_state(
                trace,
                "insufficient_evidence",
                citation_syntax="not_applicable",
                semantic_support="insufficient_evidence",
            )
        else:
            _set_response_state(
                trace,
                "answered",
                citation_syntax="not_evaluated",
                semantic_support="not_verified",
            )
        trace["latency_ms"] = int((_time.monotonic() - t0) * 1000)
        _log_ask_trace(trace)
        return answer, chunks, trace

    stage_started_at = _time.monotonic()
    query_plan = _classify_query_intent(search_query)
    _mark_stage("intent_routing", stage_started_at)
    trace["query_plan"] = query_plan
    logger.info(
        "Roteamento da pergunta: query_id=%s intent=%s modules=%s doc_types=%s",
        query_id,
        query_plan.get("intent", "general"),
        query_plan.get("modules", []),
        query_plan.get("doc_types", []),
    )

    stage_started_at = _time.monotonic()
    merged_chunks, scoped_feedback_chunks, kb_chunks = retrieve_chunks_with_feedback(
        search_query,
        query_plan=query_plan,
        scope=scope,
    )
    _mark_stage("retrieval", stage_started_at)

    stage_started_at = _time.monotonic()
    chunks = _limit_chunk_diversity(
        _rerank_chunks_with_llm(search_query, merged_chunks),
        max_per_section=config.MAX_CHUNKS_PER_SECTION,
        max_per_document=config.MAX_CHUNKS_PER_DOCUMENT,
        top_limit=config.MAX_CONTEXT_CHUNKS,
    )[:config.MAX_CONTEXT_CHUNKS]
    _mark_stage("rerank", stage_started_at)
    chunk_stats = _summarize_chunks_for_trace(chunks)
    trace.update(chunk_stats)
    trace["confidence"] = trace.get("top_similarity", 0.0)
    trace["feedback_scoped_count"] = len(scoped_feedback_chunks)
    trace["kb_chunk_count"] = len(kb_chunks)
    if scoped_feedback_chunks and kb_chunks:
        feedback_ids = list({
            str((c.get("metadata") or {}).get("feedback_item_id") or "")
            for c in scoped_feedback_chunks
            if (c.get("metadata") or {}).get("feedback_item_id")
        })
        base_sources = list({
            str(c.get("filename") or "")
            for c in kb_chunks
            if c.get("filename")
        })
        log_documentation_update_task(
            query=question,
            feedback_item_ids=feedback_ids,
            base_sources=base_sources,
            reason="Feedback escopado usado junto com base principal; revisar possivel contradicao.",
            metadata={"query_id": query_id, "platform": platform},
        )

    should_abstain, abstain_reason = _should_strict_abstain(question, chunks)
    if should_abstain and query_plan and (
        (query_plan.get("modules") or query_plan.get("doc_types"))
        and abstain_reason in {"no_chunks", "few_chunks", "low_similarity", "low_similarity_operational"}
    ):
        logger.info(
            "Abstencao inicial (motivo=%s). Tentando fallback de busca global sem filtros.",
            abstain_reason,
        )
        broad_plan = {"intent": "general", "modules": [], "doc_types": []}
        broad_merged, _broad_scoped_feedback, broad_kb = retrieve_chunks_with_feedback(
            search_query,
            query_plan=broad_plan,
            scope=scope,
        )
        combined_chunks = _dedupe_chunks(chunks + broad_merged)
        fallback_chunks = _limit_chunk_diversity(
            _rerank_chunks_with_llm(search_query, combined_chunks),
            max_per_section=config.MAX_CHUNKS_PER_SECTION,
            max_per_document=config.MAX_CHUNKS_PER_DOCUMENT,
            top_limit=config.MAX_CONTEXT_CHUNKS,
        )[:config.MAX_CONTEXT_CHUNKS]
        fallback_stats = _summarize_chunks_for_trace(fallback_chunks)
        improved = (
            fallback_stats.get("top_similarity", 0.0) > trace.get("top_similarity", 0.0)
            or fallback_stats.get("retrieved_chunk_count", 0) > trace.get("retrieved_chunk_count", 0)
        )
        if improved:
            chunks = fallback_chunks
            trace.update(fallback_stats)
            trace["confidence"] = trace.get("top_similarity", 0.0)
            trace["kb_chunk_count"] = max(int(trace.get("kb_chunk_count", 0)), len(broad_kb))
            trace["query_plan_fallback"] = "global_unfiltered"
            should_abstain, abstain_reason = _should_strict_abstain(question, chunks)
            logger.info(
                "Fallback global aplicado: top_similarity=%.3f retrieved_chunks=%d abstain=%s",
                trace.get("top_similarity", 0.0),
                trace.get("retrieved_chunk_count", 0),
                should_abstain,
            )

    if should_abstain:
        trace["abstained"] = True
        trace["abstention_reason"] = abstain_reason
        _set_response_state(
            trace,
            "insufficient_evidence",
            citation_syntax="not_applicable",
            semantic_support="insufficient_evidence",
        )
        answer = _build_abstain_response(question)
        trace["latency_ms"] = int((_time.monotonic() - t0) * 1000)
        _log_ask_trace(trace)
        return answer, chunks, trace

    stage_started_at = _time.monotonic()
    context = build_context(chunks)
    _mark_stage("context_build", stage_started_at)
    business_rules = _load_business_rules_context()
    intent_instruction = _intent_response_instruction(query_plan)
    allowed_sources = {_normalize_source_name(s) for s in trace.get("retrieved_sources", [])}
    source_display_map = {
        _normalize_source_name(str(source)): str(source)
        for source in trace.get("retrieved_sources", [])
        if str(source).strip()
    }

    system = base_system
    if business_rules:
        system += (
            "\n\n<business_rules>\n"
            "Abaixo esta o contexto FIXO de regras de negocio do maxPedido. "
            "Use essas regras como referencia canonica junto com os documentos recuperados.\n\n"
            f"{business_rules}\n"
            "</business_rules>"
        )
    if query_plan:
        routed_modules = query_plan.get("modules") or []
        if routed_modules:
            system += (
                "\n\n<routing>\n"
                f"Pergunta roteada para os modulos: {', '.join(routed_modules)}.\n"
                "Priorize contexto e exemplos desses modulos quando houver conflito de sinais."
                "\n</routing>"
            )
    if intent_instruction:
        system += (
            "\n\n<response_mode>\n"
            f"{intent_instruction}\n"
            "</response_mode>"
        )
    if config.ANALYTICAL_CONTEXT_ENABLED:
        system += (
            "\n\n<analysis_policy>\n"
            "Responda como um analista junior de suporte tecnico: conecte evidencias do contexto, "
            "explique o impacto operacional e proponha a proxima verificacao apenas quando ela estiver suportada pelo contexto. "
            "Use expressoes como 'Pela documentacao' para fatos. "
            "Nao faca inferencias fora do texto recuperado, mesmo quando o bloco analytical_context resumir a secao. "
            "Nao invente campos, telas, parametros, SQL ou procedimentos ausentes nas fontes recuperadas.\n"
            "</analysis_policy>"
        )
    if context:
        system += (
            "\n\n<context>\n"
            "Abaixo estao os trechos relevantes dos documentos da base de conhecimento. "
            "Use APENAS essas informacoes para responder. Quando houver bloco analytical_context, "
            "use-o apenas como organizacao do contexto recuperado; a evidencia continua sendo o conteudo em evidence.\n\n"
            f"{context}\n"
            "</context>"
        )
    else:
        trace["abstained"] = True
        trace["abstention_reason"] = "no_context_after_merge"
        _set_response_state(
            trace,
            "insufficient_evidence",
            citation_syntax="not_applicable",
            semantic_support="insufficient_evidence",
        )
        answer = _build_abstain_response(question)
        trace["latency_ms"] = int((_time.monotonic() - t0) * 1000)
        _log_ask_trace(trace)
        return answer, chunks, trace

    system += (
        "\n\n<citation_policy>\n"
        "Nao inclua citacoes inline no meio dos paragrafos (sem [fonte: ...] por linha).\n"
        "Use SOMENTE nomes de arquivos que estejam no contexto recuperado.\n"
        "Inclua uma secao final obrigatoria 'Fontes:' com bullets dos arquivos usados.\n"
        "Se faltarem evidencias para responder com seguranca, retorne exatamente a frase de no-answer.\n"
        "</citation_policy>"
    )
    if allowed_sources:
        system += f"\n\n<allowed_sources>{', '.join(sorted(allowed_sources))}</allowed_sources>"

    stage_started_at = _time.monotonic()
    answer = _ask_model(
        question=question,
        system=system,
        conversation_history=conversation_history,
        images=images,
    )
    _mark_stage("generation", stage_started_at)

    stage_started_at = _time.monotonic()
    answer, grounding_errors, cited_sources, regen_attempts = _apply_grounding_regeneration(
        answer=answer,
        question=question,
        system=system,
        conversation_history=conversation_history,
        images=images,
        allowed_sources=allowed_sources,
        source_display_map=source_display_map,
    )
    _mark_stage("grounding", stage_started_at)
    trace["grounding_errors"] = grounding_errors
    trace["cited_files"] = sorted(cited_sources)
    trace["citations"] = sorted(cited_sources)
    trace["regeneration_attempts"] = regen_attempts
    if _is_provider_error_response(answer):
        trace["grounding_errors"] = []
        trace["cited_files"] = []
        trace["citations"] = []
        _set_response_state(
            trace,
            "provider_error",
            citation_syntax="not_applicable",
            semantic_support="not_evaluated",
        )
    elif answer.startswith(config.NO_ANSWER_PHRASE):
        trace["abstained"] = True
        if not trace["abstention_reason"]:
            trace["abstention_reason"] = (
                "grounding_validation_failed" if grounding_errors else "model_insufficient_evidence"
            )
        _set_response_state(
            trace,
            "insufficient_evidence",
            citation_syntax="invalid" if grounding_errors else "not_applicable",
            semantic_support="insufficient_evidence",
        )
    else:
        _set_response_state(
            trace,
            "answered",
            citation_syntax="valid" if config.RAG_ENABLE_GROUNDING_VALIDATION else "not_evaluated",
            semantic_support="not_verified",
        )

    trace["latency_ms"] = int((_time.monotonic() - t0) * 1000)
    _log_ask_trace(trace)
    return answer, chunks, trace


# -- Utilitarios ----------------------------------------------------------------
def get_stats() -> dict:
    result = supabase_rpc("get_stats", {})
    if result:
        return result[0]
    return {"total_documents": 0, "total_chunks": 0}


def list_documents() -> list[dict]:
    return supabase_select(
        "documents",
        select="filename,doc_type,chunk_count,created_at",
    )


# -- Knowledge Gaps (queries sem resposta) --------------------------------------
def log_knowledge_gap(query: str, max_similarity: float, platform: str = "discord") -> None:
    """Registra query que nao teve resposta adequada na base."""
    global _knowledge_gap_rpc_available

    if _knowledge_gap_rpc_available is False:
        try:
            _fallback_log_knowledge_gap(query, max_similarity, platform)
            logger.info(
                "Knowledge gap registrado via fallback: '%s' (sim=%.2f)",
                query[:80],
                _safe_similarity(max_similarity),
            )
        except Exception as e:
            logger.warning("Erro ao registrar knowledge gap via fallback: %s", e)
        return

    try:
        supabase_rpc(
            "upsert_knowledge_gap",
            {
                "p_query": query[:500],  # limitar tamanho
                "p_max_similarity": max_similarity,
                "p_platform": platform,
            },
        )
        _knowledge_gap_rpc_available = True
        logger.info("Knowledge gap registrado: '%s' (sim=%.2f)", query[:80], max_similarity)
    except Exception as e:
        if _is_missing_rpc_function(e, "upsert_knowledge_gap"):
            if _knowledge_gap_rpc_available is not False:
                logger.warning(
                    "RPC upsert_knowledge_gap nao encontrada no PostgreSQL; usando fallback por tabela."
                )
            _knowledge_gap_rpc_available = False
            try:
                _fallback_log_knowledge_gap(query, max_similarity, platform)
                logger.info(
                    "Knowledge gap registrado via fallback: '%s' (sim=%.2f)",
                    query[:80],
                    _safe_similarity(max_similarity),
                )
            except Exception as fallback_error:
                logger.warning(
                    "Erro ao registrar knowledge gap via fallback: %s",
                    fallback_error,
                )
            return
        # Nao propagar erro: logging de gaps nao deve afetar a resposta ao usuario
        logger.warning("Erro ao registrar knowledge gap: %s", e)


def get_top_knowledge_gaps(limit: int = 10) -> list[dict]:
    """Retorna as queries sem resposta mais frequentes."""
    global _top_knowledge_gaps_rpc_available

    if _top_knowledge_gaps_rpc_available is False:
        try:
            return _fallback_get_top_knowledge_gaps(limit)
        except Exception as e:
            logger.error("Erro ao buscar knowledge gaps via fallback: %s", e)
            return []

    try:
        result = supabase_rpc("get_top_knowledge_gaps", {"p_limit": limit})
        _top_knowledge_gaps_rpc_available = True
        return result
    except Exception as e:
        if _is_missing_rpc_function(e, "get_top_knowledge_gaps"):
            if _top_knowledge_gaps_rpc_available is not False:
                logger.warning(
                    "RPC get_top_knowledge_gaps nao encontrada no PostgreSQL; usando fallback por tabela."
                )
            _top_knowledge_gaps_rpc_available = False
            try:
                return _fallback_get_top_knowledge_gaps(limit)
            except Exception as fallback_error:
                logger.error(
                    "Erro ao buscar knowledge gaps via fallback: %s",
                    fallback_error,
                )
                return []
        logger.error("Erro ao buscar knowledge gaps: %s", e)
        return []


def _extract_scalar_rpc_value(result: Any) -> str | None:
    if result is None:
        return None
    if isinstance(result, str):
        return result
    if isinstance(result, list) and result:
        first = result[0]
        if isinstance(first, dict) and first:
            first_value = next(iter(first.values()))
            if first_value is not None:
                return str(first_value)
        if isinstance(first, str):
            return first
    if isinstance(result, dict) and result:
        first_value = next(iter(result.values()))
        if first_value is not None:
            return str(first_value)
    return None


def submit_feedback_item(
    *,
    query: str,
    bot_answer: str,
    corrected_answer: str,
    tags: list[str] | None = None,
    scope: dict | None = None,
    created_by: str | None = None,
    platform: str | None = None,
    source_message_id: str | None = None,
    query_id: str | None = None,
    metadata: dict | None = None,
) -> str:
    clean_scope = _normalize_scope(scope) or {"level": "global"}
    clean_tags = [str(tag).strip() for tag in (tags or []) if str(tag).strip()]
    payload = {
        "p_query": (query or "")[:1500],
        "p_bot_answer": (bot_answer or "")[:12000],
        "p_corrected_answer": (corrected_answer or "")[:12000],
        "p_tags": clean_tags,
        "p_scope": clean_scope,
        "p_created_by": created_by,
        "p_platform": platform,
        "p_source_message_id": source_message_id,
        "p_query_id": query_id,
        "p_metadata": metadata or {},
    }
    try:
        result = supabase_rpc("submit_feedback", payload)
        scalar = _extract_scalar_rpc_value(result)
        if scalar:
            return scalar
    except Exception as e:
        if not _is_missing_rpc_function(e, "submit_feedback"):
            raise
        logger.warning("RPC submit_feedback indisponivel; usando fallback direto na tabela.")

    inserted = supabase_insert(
        "feedback_items",
        {
            "query": payload["p_query"],
            "bot_answer": payload["p_bot_answer"],
            "corrected_answer": payload["p_corrected_answer"],
            "tags": clean_tags,
            "scope": clean_scope,
            "status": "PENDING",
            "created_by": created_by,
            "platform": platform,
            "source_message_id": source_message_id,
            "query_id": query_id,
            "metadata": metadata or {},
        },
    )
    if not inserted:
        raise RuntimeError("Falha ao inserir feedback_items.")
    feedback_id = str(inserted[0]["id"])
    try:
        supabase_insert(
            "feedback_events",
            {
                "feedback_item_id": feedback_id,
                "event_type": "SUBMITTED",
                "actor": created_by,
                "payload": {"platform": platform, "query_id": query_id},
            },
        )
    except Exception:
        logger.warning("Nao foi possivel registrar evento SUBMITTED.", exc_info=True)
    return feedback_id


def list_pending_feedback_items(limit: int = 20) -> list[dict]:
    safe_limit = max(1, min(int(limit), 200))
    try:
        return supabase_rpc("list_pending_feedback", {"p_limit": safe_limit}) or []
    except Exception as e:
        if not _is_missing_rpc_function(e, "list_pending_feedback"):
            raise
        logger.warning("RPC list_pending_feedback indisponivel; usando fallback por tabela.")
    return supabase_select(
        "feedback_items",
        select="id,query,corrected_answer,tags,scope,created_by,platform,created_at",
        filters={
            "status": "eq.PENDING",
            "order": "created_at.asc",
            "limit": safe_limit,
        },
    )


def approve_feedback_item(feedback_id: str, reviewer: str | None = None, note: str | None = None) -> None:
    try:
        supabase_rpc(
            "approve_feedback",
            {"p_id": feedback_id, "p_reviewer": reviewer, "p_note": note},
        )
        return
    except Exception as e:
        if not _is_missing_rpc_function(e, "approve_feedback"):
            raise
        logger.warning("RPC approve_feedback indisponivel; usando fallback por tabela.")

    supabase_update(
        "feedback_items",
        {
            "status": "APPROVED",
            "reviewed_by": reviewer,
            "review_note": note,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        },
        {"id": f"eq.{feedback_id}"},
    )
    try:
        supabase_insert(
            "feedback_events",
            {
                "feedback_item_id": feedback_id,
                "event_type": "APPROVED",
                "actor": reviewer,
                "note": note,
            },
        )
    except Exception:
        logger.warning("Falha ao registrar evento APPROVED.", exc_info=True)


def reject_feedback_item(feedback_id: str, reviewer: str | None = None, note: str | None = None) -> None:
    try:
        supabase_rpc(
            "reject_feedback",
            {"p_id": feedback_id, "p_reviewer": reviewer, "p_note": note},
        )
        return
    except Exception as e:
        if not _is_missing_rpc_function(e, "reject_feedback"):
            raise
        logger.warning("RPC reject_feedback indisponivel; usando fallback por tabela.")

    supabase_update(
        "feedback_items",
        {
            "status": "REJECTED",
            "reviewed_by": reviewer,
            "review_note": note,
            "reviewed_at": datetime.now(timezone.utc).isoformat(),
        },
        {"id": f"eq.{feedback_id}"},
    )
    try:
        supabase_insert(
            "feedback_events",
            {
                "feedback_item_id": feedback_id,
                "event_type": "REJECTED",
                "actor": reviewer,
                "note": note,
            },
        )
    except Exception:
        logger.warning("Falha ao registrar evento REJECTED.", exc_info=True)


def _feedback_chunk_content(item: dict) -> str:
    tags = item.get("tags")
    if isinstance(tags, list):
        tags_text = ", ".join(str(tag) for tag in tags if str(tag).strip())
    else:
        tags_text = ""
    question = item.get("query") or ""
    corrected_answer = item.get("corrected_answer") or ""
    parts = [
        f"Pergunta original: {question}",
        f"Resposta corrigida: {corrected_answer}",
    ]
    if tags_text:
        parts.append(f"Tags: {tags_text}")
    return "\n".join(parts)


def publish_feedback_item(
    feedback_id: str,
    publisher: str | None = None,
    scope_override: dict | None = None,
) -> str:
    rows = supabase_select(
        "feedback_items",
        select="id,query,corrected_answer,scope,status,tags",
        filters={"id": f"eq.{feedback_id}", "limit": "1"},
    )
    if not rows:
        raise ValueError(f"Feedback nao encontrado: {feedback_id}")
    item = rows[0]
    status = str(item.get("status") or "").upper()
    if status not in {"APPROVED", "PUBLISHED"}:
        raise ValueError("Feedback precisa estar APPROVED para publicar.")

    chunk_scope = _normalize_scope(scope_override) or item.get("scope") or {"level": "global"}
    chunk_content = _feedback_chunk_content(item)
    embedding = create_document_embedding(chunk_content)
    embedding_payload = embedding_to_pgvector(embedding)

    try:
        result = supabase_rpc(
            "publish_feedback",
            {
                "p_id": feedback_id,
                "p_actor": publisher,
                "p_chunk_text": chunk_content,
                "p_scope_override": chunk_scope,
                "p_embedding": embedding_payload,
            },
        )
        scalar = _extract_scalar_rpc_value(result)
        if scalar:
            return scalar
    except Exception as e:
        if not _is_missing_rpc_function(e, "publish_feedback"):
            raise
        logger.warning("RPC publish_feedback indisponivel; usando fallback por tabela.")

    inserted = supabase_insert(
        "feedback_chunks",
        {
            "feedback_item_id": feedback_id,
            "content": chunk_content,
            "scope": chunk_scope,
            "active": True,
            "embedding": embedding_payload,
            "published_by": publisher,
        },
    )
    if not inserted:
        raise RuntimeError("Falha ao inserir feedback_chunks.")
    chunk_id = str(inserted[0]["id"])
    supabase_update(
        "feedback_items",
        {
            "status": "PUBLISHED",
            "published_at": datetime.now(timezone.utc).isoformat(),
            "reviewed_by": publisher,
        },
        {"id": f"eq.{feedback_id}"},
    )
    try:
        supabase_insert(
            "feedback_events",
            {
                "feedback_item_id": feedback_id,
                "event_type": "PUBLISHED",
                "actor": publisher,
                "payload": {"feedback_chunk_id": chunk_id},
            },
        )
    except Exception:
        logger.warning("Falha ao registrar evento PUBLISHED.", exc_info=True)
    return chunk_id


def log_documentation_update_task(
    *,
    query: str,
    feedback_item_ids: list[str] | None = None,
    base_sources: list[str] | None = None,
    reason: str = "Feedback escopado divergiu da base principal",
    metadata: dict | None = None,
) -> None:
    payload = {
        "p_query": (query or "")[:1500],
        "p_feedback_item_ids": feedback_item_ids or [],
        "p_base_sources": base_sources or [],
        "p_reason": reason,
        "p_metadata": metadata or {},
    }
    try:
        supabase_rpc("create_documentation_update_task", payload)
        return
    except Exception as e:
        if not _is_missing_rpc_function(e, "create_documentation_update_task"):
            logger.warning("Erro ao registrar documentation_update_task via RPC: %s", e)
            return
        logger.warning(
            "RPC create_documentation_update_task indisponivel; usando fallback por tabela."
        )

    try:
        supabase_insert(
            "documentation_update_tasks",
            {
                "query": payload["p_query"],
                "feedback_item_ids": payload["p_feedback_item_ids"],
                "base_sources": payload["p_base_sources"],
                "reason": payload["p_reason"],
                "status": "OPEN",
                "metadata": payload["p_metadata"],
            },
        )
    except Exception as fallback_error:
        logger.warning(
            "Erro ao registrar documentation_update_task via fallback: %s",
            fallback_error,
        )
