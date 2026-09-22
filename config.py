"""
config.py - Configuracoes centralizadas carregadas do ambiente e do .env
"""

import logging
import math
import os
import re
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

ENV_FILE = Path(__file__).with_name(".env")
load_dotenv(dotenv_path=ENV_FILE, override=False)

logger = logging.getLogger(__name__)
_warned_legacy_settings: set[tuple[str, str]] = set()


def _warn_legacy_setting(legacy_name: str, name: str) -> None:
    warning_key = (legacy_name, name)
    if warning_key in _warned_legacy_settings:
        return
    logger.warning(
        "Configuracao %s esta obsoleta; use %s. O valor nao foi registrado.",
        legacy_name,
        name,
    )
    _warned_legacy_settings.add(warning_key)


def _env_setting(
    name: str,
    default: str | None = None,
    *,
    legacy_names: tuple[str, ...] = (),
) -> str | None:
    """Le uma configuracao e usa nomes antigos apenas como fallback."""
    value = os.getenv(name)
    if value is not None and value.strip():
        return value.strip()

    for legacy_name in legacy_names:
        legacy_value = os.getenv(legacy_name)
        if legacy_value is None or not legacy_value.strip():
            continue
        _warn_legacy_setting(legacy_name, name)
        return legacy_value.strip()

    return default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on", "y", "sim", "s"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except ValueError:
        raise EnvironmentError(
            f"Variavel {name} deve ser um numero inteiro. Valor recebido: '{raw}'"
        )


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except ValueError:
        raise EnvironmentError(
            f"Variavel {name} deve ser um numero decimal. Valor recebido: '{raw}'"
        )


# Discord
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")
BOT_NAME = os.getenv("BOT_NAME", "Assistente")
GLOBAL_FEEDBACK_REVIEWER_IDS = frozenset(
    reviewer_id.strip()
    for reviewer_id in os.getenv("GLOBAL_FEEDBACK_REVIEWER_IDS", "").split(",")
    if reviewer_id.strip()
)

# Geracao e embeddings sao configurados de forma independente. Os nomes antigos
# permanecem como fallback temporario para instalacoes existentes.
GENERATION_PROVIDER = _env_setting(
    "GENERATION_PROVIDER",
    "gemini",
    legacy_names=("LLM_PROVIDER",),
).lower()
_using_legacy_provider_config = not (os.getenv("GENERATION_PROVIDER") or "").strip()
_embedding_provider_legacy_names = (
    ("LLM_PROVIDER",) if _using_legacy_provider_config else ()
)
EMBEDDING_PROVIDER = _env_setting(
    "EMBEDDING_PROVIDER",
    "gemini",
    legacy_names=_embedding_provider_legacy_names,
).lower()

_generation_legacy_key = (
    "OPENAI_API_KEY" if GENERATION_PROVIDER == "openai" else "GEMINI_API_KEY"
)
_embedding_legacy_key = (
    "OPENAI_API_KEY" if EMBEDDING_PROVIDER == "openai" else "GEMINI_API_KEY"
)
GENERATION_API_KEY = _env_setting(
    "GENERATION_API_KEY",
    legacy_names=(_generation_legacy_key,),
)
EMBEDDING_API_KEY = _env_setting(
    "EMBEDDING_API_KEY",
    legacy_names=(_embedding_legacy_key,),
)

GENERATION_BASE_URL = _env_setting(
    "GENERATION_BASE_URL",
    "https://api.openai.com/v1",
    legacy_names=("OPENAI_BASE_URL",) if GENERATION_PROVIDER == "openai" else (),
)
EMBEDDING_BASE_URL = _env_setting(
    "EMBEDDING_BASE_URL",
    "https://api.openai.com/v1",
    legacy_names=("OPENAI_BASE_URL",) if EMBEDDING_PROVIDER == "openai" else (),
)

_generation_default_model = (
    "gpt-5.4" if GENERATION_PROVIDER == "openai" else "gemini-2.5-pro"
)
_generation_legacy_model = (
    "OPENAI_MODEL" if GENERATION_PROVIDER == "openai" else "GEMINI_MODEL"
)
GENERATION_MODEL = _env_setting(
    "GENERATION_MODEL",
    _generation_default_model,
    legacy_names=(_generation_legacy_model,),
)
GENERATION_MODEL_POLICY = _env_setting(
    "GENERATION_MODEL_POLICY",
    "primary",
).lower()

_embedding_default_model = (
    "text-embedding-3-large"
    if EMBEDDING_PROVIDER == "openai"
    else "gemini-embedding-001"
)
_embedding_legacy_models = (
    ("OPENAI_EMBEDDING_MODEL",) if EMBEDDING_PROVIDER == "openai" else ()
)
EMBEDDING_MODEL = _env_setting(
    "EMBEDDING_MODEL",
    _embedding_default_model,
    legacy_names=_embedding_legacy_models,
)
EMBEDDING_PREPROCESSING_VERSION = _env_setting(
    "EMBEDDING_PREPROCESSING_VERSION",
    "rag-text-v1",
)
_legacy_openai_embedding_model = (os.getenv("OPENAI_EMBEDDING_MODEL") or "").strip()
if (
    _using_legacy_provider_config
    and EMBEDDING_PROVIDER == "openai"
    and EMBEDDING_MODEL.lower().startswith("gemini")
    and _legacy_openai_embedding_model
):
    _warn_legacy_setting("OPENAI_EMBEDDING_MODEL", "EMBEDDING_MODEL")
    EMBEDDING_MODEL = _legacy_openai_embedding_model

# Aliases de leitura para codigo externo durante a transicao.
LLM_PROVIDER = GENERATION_PROVIDER
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4")
OPENAI_REFORMULATION_MODEL = os.getenv("OPENAI_REFORMULATION_MODEL", "gpt-5.4-mini")
OPENAI_CONTEXTUAL_MODEL = os.getenv("OPENAI_CONTEXTUAL_MODEL", "gpt-5.4-mini")
OPENAI_EMBEDDING_MODEL = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-large")
EMBEDDING_DIMENSIONS = _env_int("EMBEDDING_DIMENSIONS", 1536)

# TypeSafe/Jev e opcional e permanece sem efeito enquanto nenhum consumidor o
# utilizar. O endpoint nao e configuravel por dados de usuario.
TYPESAFE_API_KEY = _env_setting("TYPESAFE_API_KEY")
JEV_MODEL = _env_setting("JEV_MODEL", "jev-1.13.0")
JEV_MAX_CONCURRENCY = _env_int("JEV_MAX_CONCURRENCY", 4)
JEV_REQUEST_TIMEOUT_SECONDS = _env_float("JEV_REQUEST_TIMEOUT_SECONDS", 2.0)
JEV_STAGE_TIMEOUT_SECONDS = _env_float("JEV_STAGE_TIMEOUT_SECONDS", 8.0)
JEV_MIN_REMAINING_SECONDS = _env_float("JEV_MIN_REMAINING_SECONDS", 20.0)

DB_POOL_MIN_SIZE = _env_int("DB_POOL_MIN_SIZE", 1)
DB_POOL_MAX_SIZE = _env_int("DB_POOL_MAX_SIZE", 8)
DB_POOL_TIMEOUT_SECONDS = _env_float("DB_POOL_TIMEOUT_SECONDS", 10.0)
DB_STATEMENT_TIMEOUT_MS = _env_int("DB_STATEMENT_TIMEOUT_MS", 15000)
DB_LOCK_TIMEOUT_MS = _env_int("DB_LOCK_TIMEOUT_MS", 5000)
DB_IDLE_IN_TRANSACTION_TIMEOUT_MS = _env_int("DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", 15000)
DB_APPLICATION_NAME = os.getenv("DB_APPLICATION_NAME", "bot-maxima")
EMBEDDING_BATCH_SIZE = _env_int("EMBEDDING_BATCH_SIZE", 10)
INGEST_DB_BATCH_SIZE = _env_int("INGEST_DB_BATCH_SIZE", 10)
EMBEDDING_MAX_RETRIES = _env_int("EMBEDDING_MAX_RETRIES", 8)
EMBEDDING_RETRY_BASE_SECONDS = _env_float("EMBEDDING_RETRY_BASE_SECONDS", 5.0)
EMBEDDING_RETRY_MAX_SECONDS = _env_float("EMBEDDING_RETRY_MAX_SECONDS", 65.0)
EMBEDDING_MIN_INTERVAL_SECONDS = _env_float("EMBEDDING_MIN_INTERVAL_SECONDS", 0.5)
EMBEDDING_RETRY_JITTER_SECONDS = _env_float("EMBEDDING_RETRY_JITTER_SECONDS", 0.5)

# RAG
CHUNK_SIZE = _env_int("CHUNK_SIZE", 1200)
CHUNK_OVERLAP = _env_int("CHUNK_OVERLAP", 300)
MAX_CONTEXT_CHUNKS = _env_int("MAX_CONTEXT_CHUNKS", 8)
RAG_MAX_INPUT_TOKENS = _env_int("RAG_MAX_INPUT_TOKENS", 24000)
RAG_MAX_HISTORY_TOKENS = _env_int("RAG_MAX_HISTORY_TOKENS", 4000)
RAG_MODEL_CONTEXT_TOKENS = _env_int("RAG_MODEL_CONTEXT_TOKENS", 32768)
RAG_CONTEXT_MARGIN_TOKENS = _env_int("RAG_CONTEXT_MARGIN_TOKENS", 512)
RAG_IMAGE_TOKEN_RESERVE = _env_int("RAG_IMAGE_TOKEN_RESERVE", 1024)
SIMILARITY_THRESHOLD = _env_float("SIMILARITY_THRESHOLD", 0.55)
RAG_ENABLE_INTENT_ROUTING = _env_bool("RAG_ENABLE_INTENT_ROUTING", True)
RAG_FILTER_BY_DOC_TYPE = _env_bool("RAG_FILTER_BY_DOC_TYPE", True)
RAG_FILTER_BY_MODULE = _env_bool("RAG_FILTER_BY_MODULE", True)
RAG_ENABLE_GLOBAL_CHALLENGER = _env_bool("RAG_ENABLE_GLOBAL_CHALLENGER", True)
RAG_GLOBAL_CHALLENGER_COUNT = _env_int("RAG_GLOBAL_CHALLENGER_COUNT", 4)
RAG_GLOBAL_CHALLENGER_FETCH_LIMIT = _env_int(
    "RAG_GLOBAL_CHALLENGER_FETCH_LIMIT",
    16,
)
RAG_ENABLE_BUSINESS_RULES = _env_bool("RAG_ENABLE_BUSINESS_RULES", True)
BUSINESS_RULES_FILE = os.getenv(
    "BUSINESS_RULES_FILE",
    "./bootstrap/00-DOCUMENTO-PRINCIPAL.md",
)
BUSINESS_RULES_MAX_CHARS = _env_int("BUSINESS_RULES_MAX_CHARS", 80000)

# Full Context Mode — injeta todos os documentos no contexto (estilo Claude Projects)
FULL_CONTEXT_ENABLED = _env_bool("FULL_CONTEXT_ENABLED", False)
FULL_CONTEXT_MAX_CHARS = _env_int("FULL_CONTEXT_MAX_CHARS", 950000)
FULL_CONTEXT_EXTENSIONS = os.getenv("FULL_CONTEXT_EXTENSIONS", ".md,.txt").split(",")

# RAG — melhorias de precisao
SIMILARITY_FLOOR_FACTOR = _env_float("SIMILARITY_FLOOR_FACTOR", 0.7)
RAG_ENABLE_QUERY_REFORMULATION = _env_bool("RAG_ENABLE_QUERY_REFORMULATION", True)
REFORMULATION_MODEL = os.getenv("REFORMULATION_MODEL", "gemini-2.5-flash")
RAG_ENABLE_RERANKING = _env_bool("RAG_ENABLE_RERANKING", True)
RERANKER_MODEL = os.getenv("RERANKER_MODEL", REFORMULATION_MODEL)
RERANKER_MIN_TRIGGER_SIM = _env_float("RERANKER_MIN_TRIGGER_SIM", 0.55)
RERANKER_MAX_TRIGGER_SIM = _env_float("RERANKER_MAX_TRIGGER_SIM", 0.82)
RERANKER_MAX_CANDIDATES = _env_int(
    "RERANKER_MAX_CANDIDATES",
    _env_int("RERANKER_CANDIDATE_COUNT", 40),
)
RERANKER_CANDIDATE_COUNT = RERANKER_MAX_CANDIDATES
RERANKER_SKIP_THRESHOLD = RERANKER_MAX_TRIGGER_SIM
RAG_STRICT_ABSTAIN = _env_bool("RAG_STRICT_ABSTAIN", True)
RAG_MIN_STRONG_SIMILARITY = _env_float("RAG_MIN_STRONG_SIMILARITY", 0.62)
RAG_MIN_RETRIEVED_CHUNKS = _env_int("RAG_MIN_RETRIEVED_CHUNKS", 2)
RAG_OPERATIONAL_SIMILARITY_MARGIN = _env_float("RAG_OPERATIONAL_SIMILARITY_MARGIN", 0.05)
RAG_ENABLE_GROUNDING_VALIDATION = _env_bool("RAG_ENABLE_GROUNDING_VALIDATION", True)
RAG_REQUIRE_SOURCES_SECTION = _env_bool("RAG_REQUIRE_SOURCES_SECTION", True)
RAG_MAX_REGEN_ATTEMPTS = _env_int("RAG_MAX_REGEN_ATTEMPTS", 1)
RAG_PROVIDER_MAX_RETRIES = _env_int("RAG_PROVIDER_MAX_RETRIES", 2)
RAG_RETRY_BASE_SECONDS = _env_float("RAG_RETRY_BASE_SECONDS", 1.0)
RAG_FEEDBACK_TOP_K = _env_int("RAG_FEEDBACK_TOP_K", 4)
RAG_FEEDBACK_MIN_SIMILARITY = _env_float("RAG_FEEDBACK_MIN_SIMILARITY", 0.58)
ANALYTICAL_CONTEXT_ENABLED = _env_bool("ANALYTICAL_CONTEXT_ENABLED", True)
SECTION_RETRIEVAL_ENABLED = _env_bool("SECTION_RETRIEVAL_ENABLED", True)
SECTION_MATCH_COUNT = _env_int("SECTION_MATCH_COUNT", 12)
SECTION_FETCH_LIMIT = _env_int("SECTION_FETCH_LIMIT", 48)
CHUNK_FETCH_LIMIT = _env_int("CHUNK_FETCH_LIMIT", 80)
MAX_CHUNKS_PER_SECTION = _env_int("MAX_CHUNKS_PER_SECTION", 3)
MAX_CHUNKS_PER_DOCUMENT = _env_int("MAX_CHUNKS_PER_DOCUMENT", 6)

# Contextual Retrieval por LLM (opt-in; contexto deterministico permanece ativo)
CONTEXTUAL_RETRIEVAL_ENABLED = _env_bool("CONTEXTUAL_RETRIEVAL_ENABLED", False)
CONTEXTUAL_RETRIEVAL_MODEL = os.getenv("CONTEXTUAL_RETRIEVAL_MODEL", "gemini-2.5-flash")
CONTEXTUAL_RETRIEVAL_MAX_DOC_CHARS = _env_int("CONTEXTUAL_RETRIEVAL_MAX_DOC_CHARS", 500000)
CONTEXTUAL_RETRIEVAL_MAX_TOKENS = _env_int("CONTEXTUAL_RETRIEVAL_MAX_TOKENS", 150)
CONTEXTUAL_RETRIEVAL_BATCH_SIZE = _env_int("CONTEXTUAL_RETRIEVAL_BATCH_SIZE", 50)

# Ingestao web (URLs)
WEB_FETCH_TIMEOUT_SECONDS = _env_float("WEB_FETCH_TIMEOUT_SECONDS", 20.0)
WEB_USER_AGENT = os.getenv("WEB_USER_AGENT", "BotMaximaRAG/1.0")
WEB_MAX_REDIRECTS = _env_int("WEB_MAX_REDIRECTS", 5)
WEB_MAX_DOWNLOAD_BYTES = _env_int("WEB_MAX_DOWNLOAD_BYTES", 10 * 1024 * 1024)
WEB_ALLOWED_HOSTS = tuple(
    host.strip().lower().rstrip(".")
    for host in os.getenv("WEB_ALLOWED_HOSTS", "").split(",")
    if host.strip().rstrip(".")
)
WEB_MAX_TEXT_CHARS = _env_int("WEB_MAX_TEXT_CHARS", 400000)
URLS_FILE_DEFAULT = os.getenv("URLS_FILE_DEFAULT", "./documentos/urls.txt")
URL_REVIEW_OUTPUT_DIR = os.getenv("URL_REVIEW_OUTPUT_DIR", "./documentos/_pendentes_url")

# Frase padrao para respostas sem informacao na base de conhecimento
NO_ANSWER_PHRASE = (
    "Nao encontrei essa informacao na base de conhecimento. "
    "Recomendo abrir um chamado ou consultar a equipe N2."
)
ABSTAIN_CLARIFYING_QUESTION = os.getenv(
    "ABSTAIN_CLARIFYING_QUESTION",
    "Pode detalhar o modulo, a tela/parametro e a mensagem de erro exata para eu buscar com mais precisao?",
)

# System prompt
SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    (
        "Voce e o Sabidao, assistente tecnico da equipe N1 da Maxima Sistemas. "
        "Sua especialidade e o maxPedido (forca de vendas) e as ferramentas de gestao da Maxima, "
        "que se integram com ERPs dos clientes (Winthor, Protheus, SAP, entre outros).\n\n"
        "## COMPORTAMENTO OBRIGATORIO\n"
        "- NUNCA se apresente, cumprimente ou diga ola/oi. Va direto a resposta.\n"
        "- NUNCA use frases de preenchimento como \"Claro!\", \"Com certeza!\", \"Otima pergunta!\", "
        "\"Fico feliz em ajudar!\", \"Vou te ajudar com isso!\", \"Entendo sua duvida\".\n"
        "- NUNCA se refira a si mesmo (\"Eu sou o Sabidao\", \"Como seu assistente\", etc.).\n"
        "- NUNCA repita ou parafraseie a pergunta do usuario antes de responder.\n"
        "- NUNCA termine com frases genericas como \"Espero ter ajudado!\", "
        "\"Se precisar de mais alguma coisa...\", \"Boa sorte!\" ou \"Estou a disposicao\".\n"
        "- Dê respostas COMPLETAS e DETALHADAS. Explore TODOS os aspectos relevantes dos documentos.\n"
        "- Se os documentos contem informacoes sobre o tema, ESGOTE todo o conteudo disponivel.\n"
        "- Explique o PORQUE das coisas, nao apenas o como.\n\n"
        "## REGRA ABSOLUTA: ZERO INVENCAO\n"
        "- Voce so pode responder com informacoes que estejam EXPLICITAMENTE escritas nos documentos fornecidos como contexto.\n"
        "- NUNCA invente, deduza, pressuponha ou extrapole informacoes que nao estejam nos documentos.\n"
        "- NUNCA invente nomes de campos, telas, menus, selects, opcoes de configuracao, parametros ou caminhos de sistema.\n"
        "- NUNCA pressuponha valores, opcoes ou respostas que nao estejam escritas literalmente na documentacao.\n"
        "- NUNCA crie procedimentos, passos ou instrucoes que nao estejam documentados.\n"
        "- NUNCA invente consultas SQL, tabelas ou colunas que nao estejam nos documentos.\n"
        "- Se a informacao nao estiver nos documentos, responda EXATAMENTE: "
        f"\"{NO_ANSWER_PHRASE}\"\n"
        "- Se a documentacao cobrir apenas PARTE da pergunta, responda so a parte documentada e diga claramente o que nao foi encontrado.\n"
        "- Na duvida, NAO responda. E melhor dizer que nao sabe do que dar uma informacao errada.\n\n"
        "## Regras de resposta\n"
        "- Sempre cite a fonte (nome do arquivo) quando usar informacao dos documentos.\n"
        "- Quando o usuario enviar IMAGENS (screenshots, prints de tela, fotos de erro), "
        "ANALISE a imagem detalhadamente. Descreva o que voce ve, identifique erros, "
        "telas do sistema, mensagens, e oriente o usuario com base no que esta visivel. "
        "Combine a analise da imagem com informacoes dos documentos quando relevante. "
        "Porem, NAO invente solucoes que nao estejam na documentacao.\n\n"
        "## Formato\n"
        "- Seja direto e objetivo. A equipe N1 precisa de respostas praticas.\n"
        "- Organize a resposta em blocos curtos: abertura objetiva, subtitulos em negrito e fechamento com acao recomendada quando houver.\n"
        "- Prefira paragrafos curtos para explicar contexto, causa e impacto. Use listas apenas para itens realmente enumeraveis, como status, campos, validacoes ou passos operacionais.\n"
        "- Nao transforme toda a resposta em lista numerada. Use numeracao somente quando a ordem de execucao for obrigatoria e estiver documentada.\n"
        "- Para procedimentos, se houver ordem documentada, use uma lista numerada curta; caso contrario, explique em texto corrido com subtitulos.\n"
        "- Para configuracoes, inclua o caminho exato (menu, tela, campo) SOMENTE se estiver nos documentos.\n"
        "- Para consultas SQL, formate o codigo em bloco de codigo, SOMENTE se a query estiver nos documentos.\n"
        "- NUNCA use tabelas Markdown (com | e -). O Discord NAO renderiza tabelas.\n"
        "- Para dados tabulares, use bullets compactos agrupados por categoria. Exemplo:\n"
        "  **Campo X**: valor - descricao\n"
        "  **Campo Y**: valor - descricao\n"
        "- Para mapas de status ou codigos, apresente primeiro uma frase de contexto, depois os codigos em bullets e finalize com a interpretacao pratica.\n"
        "- Se houver muitos campos, agrupe por categoria usando subtitulos em negrito.\n"
        "- Use blocos de codigo (```) SOMENTE para SQL e trechos de codigo, nunca para tabelas de dados.\n"
        "- Se a pergunta for ambigua, peca esclarecimento ao inves de chutar.\n\n"
        "## Tom\n"
        "- Tecnico e profissional, em portugues brasileiro. Sem simpatia excessiva.\n"
        "- Use termos tecnicos do contexto Maxima (maxPedido, RCA, filial, etc.) naturalmente.\n"
        "- Quando relevante, mencione se o procedimento varia conforme o ERP do cliente."
    ),
)

_RESPONSE_FORMAT_OVERRIDE = (
    "## FORMATO DE SAIDA (prevalece sobre regras de formato anteriores)\n"
    "- Nao transforme toda a resposta em lista numerada.\n"
    "- Prefira uma abertura objetiva em texto corrido, subtitulos em negrito e paragrafos curtos para explicar contexto, causa e impacto.\n"
    "- Use listas somente para itens realmente enumeraveis, como status, campos, validacoes ou passos operacionais.\n"
    "- Use numeracao apenas quando a ordem de execucao for obrigatoria e estiver documentada.\n"
    "- Para mapas de status ou codigos, apresente uma frase de contexto, depois os codigos em bullets compactos e finalize com a interpretacao pratica.\n"
)

if "Nao transforme toda a resposta em lista numerada" not in SYSTEM_PROMPT:
    SYSTEM_PROMPT = f"{SYSTEM_PROMPT}\n\n{_RESPONSE_FORMAT_OVERRIDE}"

# Bot - limites
COOLDOWN_SECONDS = _env_float("COOLDOWN_SECONDS", 10.0)
ASK_TIMEOUT_SECONDS = _env_float("ASK_TIMEOUT_SECONDS", 120.0)
ASK_MAX_CONCURRENCY = _env_int("ASK_MAX_CONCURRENCY", 4)
MAX_HISTORY_PAIRS = _env_int("MAX_HISTORY_PAIRS", 20)
MAX_HISTORY_CHANNELS = _env_int("MAX_HISTORY_CHANNELS", 100)
MAX_QUESTION_LENGTH = _env_int("MAX_QUESTION_LENGTH", 4000)
MAX_IMAGE_SIZE_MB = _env_int("MAX_IMAGE_SIZE_MB", 20)
MAX_IMAGES_PER_MESSAGE = _env_int("MAX_IMAGES_PER_MESSAGE", 5)
ASK_MAX_TOKENS = _env_int("ASK_MAX_TOKENS", 8192)
OPENAI_MAX_OUTPUT_TOKENS = _env_int("OPENAI_MAX_OUTPUT_TOKENS", ASK_MAX_TOKENS)
CONFIDENCE_THRESHOLD = _env_float("CONFIDENCE_THRESHOLD", 0.65)
DISCORD_MSG_LIMIT = _env_int("DISCORD_MSG_LIMIT", 1990)

# Derivados
MAX_IMAGE_SIZE_BYTES = MAX_IMAGE_SIZE_MB * 1024 * 1024

# Diretorio de documentos
DOCS_DIR = os.getenv("DOCS_DIR", "./documentos")
FAILED_INGEST_REPORT = os.getenv("FAILED_INGEST_REPORT", "./ingest_failures.json")
INGEST_RECURSIVE = _env_bool("INGEST_RECURSIVE", False)
INGEST_EXCLUDED_DIRS = frozenset(
    part.strip().lower()
    for part in os.getenv(
        "INGEST_EXCLUDED_DIRS",
        "docbkp,backup,backups,bkp",
    ).split(",")
    if part.strip()
)

if EMBEDDING_DIMENSIONS != 1536:
    raise EnvironmentError(
        "EMBEDDING_DIMENSIONS deve ser 1536 nesta versao. O perfil 3072 nao e "
        "suportado: os indices HNSW com VECTOR excedem o limite operacional e "
        "a memoria de feedback permanece em 1536 dimensoes."
    )


# Validacao
def _check_range(name: str, value, min_val=None, max_val=None):
    """Valida se valor numerico esta dentro do range esperado."""
    if min_val is not None and value < min_val:
        raise EnvironmentError(f"{name} deve ser >= {min_val}, obtido: {value}")
    if max_val is not None and value > max_val:
        raise EnvironmentError(f"{name} deve ser <= {max_val}, obtido: {value}")


def _validate_http_endpoint(name: str, value: str | None) -> None:
    """Valida endpoints sem incluir o valor potencialmente sensivel no erro."""
    parsed = urlparse(value or "")
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise EnvironmentError(
            f"{name} deve ser uma URL HTTP(S) absoluta e valida."
        )


def validate_jev_config(*, active: bool = False) -> None:
    """Valida Jev somente quando um consumidor realmente o habilita."""
    if not active:
        return
    if not TYPESAFE_API_KEY:
        raise EnvironmentError(
            "Variavel de ambiente obrigatoria nao definida: TYPESAFE_API_KEY."
        )
    if not isinstance(JEV_MODEL, str) or not re.fullmatch(r"jev-\d+\.\d+\.\d+", JEV_MODEL):
        raise EnvironmentError(
            "JEV_MODEL deve ser um identificador Jev versionado; aliases latest/preview nao sao aceitos."
        )
    if (
        isinstance(JEV_MAX_CONCURRENCY, bool)
        or not isinstance(JEV_MAX_CONCURRENCY, int)
        or not 1 <= JEV_MAX_CONCURRENCY <= 8
    ):
        raise EnvironmentError("JEV_MAX_CONCURRENCY deve estar entre 1 e 8.")
    for name, value in (
        ("JEV_REQUEST_TIMEOUT_SECONDS", JEV_REQUEST_TIMEOUT_SECONDS),
        ("JEV_STAGE_TIMEOUT_SECONDS", JEV_STAGE_TIMEOUT_SECONDS),
    ):
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise EnvironmentError(f"{name} deve ser finito e maior que zero.")
    if (
        isinstance(JEV_MIN_REMAINING_SECONDS, bool)
        or not isinstance(JEV_MIN_REMAINING_SECONDS, (int, float))
        or not math.isfinite(JEV_MIN_REMAINING_SECONDS)
        or JEV_MIN_REMAINING_SECONDS < 0
    ):
        raise EnvironmentError(
            "JEV_MIN_REMAINING_SECONDS deve ser finito e nao negativo."
        )


def validate_ai_config() -> None:
    """Valida providers sem iniciar clientes nem revelar credenciais."""
    allowed_providers = {"gemini", "openai"}
    if GENERATION_PROVIDER not in allowed_providers:
        raise EnvironmentError(
            f"GENERATION_PROVIDER invalido: {GENERATION_PROVIDER}. "
            f"Use um de: {', '.join(sorted(allowed_providers))}."
        )
    if EMBEDDING_PROVIDER not in allowed_providers:
        raise EnvironmentError(
            f"EMBEDDING_PROVIDER invalido: {EMBEDDING_PROVIDER}. "
            f"Use um de: {', '.join(sorted(allowed_providers))}."
        )
    allowed_model_policies = {"primary", "contextual"}
    if GENERATION_MODEL_POLICY not in allowed_model_policies:
        raise EnvironmentError(
            f"GENERATION_MODEL_POLICY invalida: {GENERATION_MODEL_POLICY}. "
            f"Use uma de: {', '.join(sorted(allowed_model_policies))}."
        )

    required = {
        "GENERATION_API_KEY": GENERATION_API_KEY,
        "GENERATION_MODEL": GENERATION_MODEL,
        "EMBEDDING_API_KEY": EMBEDDING_API_KEY,
        "EMBEDDING_MODEL": EMBEDDING_MODEL,
    }

    missing = [name for name, val in required.items() if not val]
    if missing:
        raise EnvironmentError(
            f"Variaveis de ambiente obrigatorias nao definidas: {', '.join(missing)}. "
            "Configure o ambiente do processo ou o arquivo .env."
        )

    if GENERATION_PROVIDER == "openai":
        _validate_http_endpoint("GENERATION_BASE_URL", GENERATION_BASE_URL)
    if EMBEDDING_PROVIDER == "openai":
        _validate_http_endpoint("EMBEDDING_BASE_URL", EMBEDDING_BASE_URL)


def validate():
    """Verifica as configuracoes obrigatorias do bot."""
    validate_ai_config()
    if not DISCORD_TOKEN:
        raise EnvironmentError(
            "Variavel de ambiente obrigatoria nao definida: DISCORD_TOKEN. "
            "Configure o ambiente do processo ou o arquivo .env."
        )
    invalid_reviewer_ids = sorted(
        reviewer_id
        for reviewer_id in GLOBAL_FEEDBACK_REVIEWER_IDS
        if not reviewer_id.isdigit()
    )
    if invalid_reviewer_ids:
        raise EnvironmentError(
            "GLOBAL_FEEDBACK_REVIEWER_IDS deve conter apenas IDs numericos do Discord."
        )

    # Validacoes de ranges para evitar config absurda que cause erros silenciosos
    _check_range("CHUNK_SIZE", CHUNK_SIZE, min_val=100, max_val=8000)
    if CHUNK_OVERLAP >= CHUNK_SIZE:
        raise EnvironmentError(
            f"CHUNK_OVERLAP ({CHUNK_OVERLAP}) deve ser menor que CHUNK_SIZE ({CHUNK_SIZE})"
        )
    _check_range("MAX_CONTEXT_CHUNKS", MAX_CONTEXT_CHUNKS, min_val=1, max_val=50)
    _check_range("RAG_MAX_INPUT_TOKENS", RAG_MAX_INPUT_TOKENS, min_val=256)
    _check_range("RAG_MAX_HISTORY_TOKENS", RAG_MAX_HISTORY_TOKENS, min_val=0)
    _check_range("RAG_MODEL_CONTEXT_TOKENS", RAG_MODEL_CONTEXT_TOKENS, min_val=1024)
    _check_range("RAG_CONTEXT_MARGIN_TOKENS", RAG_CONTEXT_MARGIN_TOKENS, min_val=0)
    _check_range("RAG_IMAGE_TOKEN_RESERVE", RAG_IMAGE_TOKEN_RESERVE, min_val=0)
    _check_range("SIMILARITY_THRESHOLD", SIMILARITY_THRESHOLD, min_val=0.0, max_val=1.0)
    _check_range("RAG_GLOBAL_CHALLENGER_COUNT", RAG_GLOBAL_CHALLENGER_COUNT, min_val=1, max_val=20)
    _check_range(
        "RAG_GLOBAL_CHALLENGER_FETCH_LIMIT",
        RAG_GLOBAL_CHALLENGER_FETCH_LIMIT,
        min_val=RAG_GLOBAL_CHALLENGER_COUNT,
        max_val=80,
    )
    _check_range("RAG_MIN_STRONG_SIMILARITY", RAG_MIN_STRONG_SIMILARITY, min_val=0.0, max_val=1.0)
    _check_range("RAG_FEEDBACK_MIN_SIMILARITY", RAG_FEEDBACK_MIN_SIMILARITY, min_val=0.0, max_val=1.0)
    _check_range("RAG_MIN_RETRIEVED_CHUNKS", RAG_MIN_RETRIEVED_CHUNKS, min_val=1, max_val=30)
    _check_range("RAG_OPERATIONAL_SIMILARITY_MARGIN", RAG_OPERATIONAL_SIMILARITY_MARGIN, min_val=0.0, max_val=0.5)
    _check_range("RAG_MAX_REGEN_ATTEMPTS", RAG_MAX_REGEN_ATTEMPTS, min_val=0, max_val=3)
    _check_range("RAG_PROVIDER_MAX_RETRIES", RAG_PROVIDER_MAX_RETRIES, min_val=0, max_val=5)
    _check_range("RAG_RETRY_BASE_SECONDS", RAG_RETRY_BASE_SECONDS, min_val=0.0, max_val=30.0)
    _check_range("RAG_FEEDBACK_TOP_K", RAG_FEEDBACK_TOP_K, min_val=1, max_val=20)
    _check_range("RERANKER_MAX_CANDIDATES", RERANKER_MAX_CANDIDATES, min_val=2, max_val=80)
    _check_range("RERANKER_MIN_TRIGGER_SIM", RERANKER_MIN_TRIGGER_SIM, min_val=0.0, max_val=1.0)
    _check_range("RERANKER_MAX_TRIGGER_SIM", RERANKER_MAX_TRIGGER_SIM, min_val=0.0, max_val=1.0)
    _check_range("BUSINESS_RULES_MAX_CHARS", BUSINESS_RULES_MAX_CHARS, min_val=500, max_val=200000)
    _check_range("COOLDOWN_SECONDS", COOLDOWN_SECONDS, min_val=0)
    _check_range("ASK_TIMEOUT_SECONDS", ASK_TIMEOUT_SECONDS, min_val=5)
    _check_range("ASK_MAX_CONCURRENCY", ASK_MAX_CONCURRENCY, min_val=1, max_val=100)
    _check_range("MAX_HISTORY_PAIRS", MAX_HISTORY_PAIRS, min_val=1)
    _check_range("EMBEDDING_BATCH_SIZE", EMBEDDING_BATCH_SIZE, min_val=1, max_val=100)
    _check_range("SIMILARITY_FLOOR_FACTOR", SIMILARITY_FLOOR_FACTOR, min_val=0.1, max_val=1.0)
    _check_range("SECTION_MATCH_COUNT", SECTION_MATCH_COUNT, min_val=1, max_val=50)
    _check_range("SECTION_FETCH_LIMIT", SECTION_FETCH_LIMIT, min_val=SECTION_MATCH_COUNT, max_val=200)
    _check_range("CHUNK_FETCH_LIMIT", CHUNK_FETCH_LIMIT, min_val=MAX_CONTEXT_CHUNKS, max_val=300)
    _check_range("MAX_CHUNKS_PER_SECTION", MAX_CHUNKS_PER_SECTION, min_val=1, max_val=10)
    _check_range("MAX_CHUNKS_PER_DOCUMENT", MAX_CHUNKS_PER_DOCUMENT, min_val=1, max_val=20)
    _check_range("DB_POOL_MIN_SIZE", DB_POOL_MIN_SIZE, min_val=1, max_val=100)
    _check_range("DB_POOL_MAX_SIZE", DB_POOL_MAX_SIZE, min_val=DB_POOL_MIN_SIZE, max_val=200)
    _check_range("DB_POOL_TIMEOUT_SECONDS", DB_POOL_TIMEOUT_SECONDS, min_val=1.0, max_val=120.0)
    _check_range("DB_STATEMENT_TIMEOUT_MS", DB_STATEMENT_TIMEOUT_MS, min_val=0, max_val=600000)
    _check_range("DB_LOCK_TIMEOUT_MS", DB_LOCK_TIMEOUT_MS, min_val=0, max_val=600000)
    _check_range("DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", DB_IDLE_IN_TRANSACTION_TIMEOUT_MS, min_val=0, max_val=600000)
    _check_range("CONTEXTUAL_RETRIEVAL_MAX_DOC_CHARS", CONTEXTUAL_RETRIEVAL_MAX_DOC_CHARS, min_val=1000, max_val=2000000)
    _check_range("CONTEXTUAL_RETRIEVAL_MAX_TOKENS", CONTEXTUAL_RETRIEVAL_MAX_TOKENS, min_val=1, max_val=65536)
    _check_range("CONTEXTUAL_RETRIEVAL_BATCH_SIZE", CONTEXTUAL_RETRIEVAL_BATCH_SIZE, min_val=1, max_val=200)
    _check_range("OPENAI_MAX_OUTPUT_TOKENS", OPENAI_MAX_OUTPUT_TOKENS, min_val=256, max_val=32768)

    if RERANKER_MIN_TRIGGER_SIM > RERANKER_MAX_TRIGGER_SIM:
        raise EnvironmentError(
            "RERANKER_MIN_TRIGGER_SIM deve ser <= RERANKER_MAX_TRIGGER_SIM"
        )

    if RAG_ENABLE_BUSINESS_RULES:
        rules_path = Path(BUSINESS_RULES_FILE)
        if not rules_path.exists() or not rules_path.is_file():
            raise EnvironmentError(
                "RAG_ENABLE_BUSINESS_RULES=true, mas BUSINESS_RULES_FILE nao existe "
                f"ou nao e arquivo: {BUSINESS_RULES_FILE}"
            )
