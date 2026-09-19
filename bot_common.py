"""
bot_common.py — Utilitarios de conversa e formatacao do bot Discord.
Evita duplicacao de codigo de historico, cooldown, split e formatacao.
"""

import asyncio
import re
import time
import unicodedata
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import config

_NUMBERED_LINE_RE = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.+?)\s*$")
_SOURCES_HEADING_RE = re.compile(r"^\s*fontes?\s*:\s*$", re.IGNORECASE)
_TITLE_PREFIX_RE = re.compile(
    r"^(como|quando|onde|por que|porque|causa|causas|solucao|solucoes|"
    r"diagnostico|validacao|requisitos|parametros|fluxo|resumo|"
    r"observacoes|tabelas|campos|status)\b",
    re.IGNORECASE,
)


@dataclass
class _ConversationState:
    history: list[dict] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class ConversationManager:
    """Serializa e limita historicos por chave de conversa com evicao LRU."""

    def __init__(self, max_conversations: int | None = None):
        self._max_conversations = max_conversations or config.MAX_HISTORY_CHANNELS
        self._states: OrderedDict[Any, _ConversationState] = OrderedDict()
        self._user_last_ask: dict = {}

    @property
    def conversation_count(self) -> int:
        return len(self._states)

    def _get_or_create_state(self, conversation_key: Any) -> _ConversationState:
        state = self._states.get(conversation_key)
        if state is None:
            state = _ConversationState()
            self._states[conversation_key] = state
        else:
            self._states.move_to_end(conversation_key)
        return state

    def _evict_inactive(self) -> None:
        while len(self._states) > self._max_conversations:
            evicted = False
            for key, state in self._states.items():
                if state.users == 0 and not state.lock.locked():
                    del self._states[key]
                    evicted = True
                    break
            if not evicted:
                break

    @asynccontextmanager
    async def serialized(self, conversation_key: Any):
        """Mantem uma operacao por conversa sem bloquear conversas diferentes."""
        state = self._get_or_create_state(conversation_key)
        state.users += 1
        self._evict_inactive()
        try:
            async with state.lock:
                self._states.move_to_end(conversation_key)
                yield
        finally:
            state.users -= 1
            if conversation_key in self._states:
                self._states.move_to_end(conversation_key)
            self._evict_inactive()

    def get_history_snapshot(self, conversation_key: Any) -> list[dict]:
        """Copia o historico atual; a lista interna nunca escapa do gerenciador."""
        state = self._get_or_create_state(conversation_key)
        return [dict(message) for message in state.history]

    def append_exchange(
        self,
        conversation_key: Any,
        question: str,
        answer: str,
    ) -> None:
        """Inclui um par e aplica trim sem substituir a lista interna."""
        state = self._get_or_create_state(conversation_key)
        state.history.extend(
            (
                {"role": "user", "content": question},
                {"role": "assistant", "content": answer},
            )
        )
        max_messages = config.MAX_HISTORY_PAIRS * 2
        if len(state.history) > max_messages:
            del state.history[:-max_messages]

    def clear_history(self, conversation_key: Any) -> None:
        """Limpa em lugar para não invalidar uma operação que já usa o estado."""
        state = self._states.get(conversation_key)
        if state is not None:
            state.history.clear()

    def check_cooldown(self, user_id) -> float | None:
        """Retorna segundos restantes se em cooldown, senao None."""
        last = self._user_last_ask.get(user_id, 0)
        remaining = config.COOLDOWN_SECONDS - (time.monotonic() - last)
        if remaining > 0:
            return remaining
        self._user_last_ask[user_id] = time.monotonic()
        return None


class InFlightTaskLimiter:
    """Admite trabalho local ate um limite e conta a execucao real da task.

    O chamador pode abandonar a espera com ``asyncio.shield`` sem liberar a
    vaga. A vaga so volta a ficar disponivel quando a task termina de fato.
    """

    def __init__(self, max_concurrency: int):
        if max_concurrency < 1:
            raise ValueError("max_concurrency deve ser pelo menos 1")
        self._max_concurrency = max_concurrency
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    def try_start(
        self,
        coroutine_factory: Callable[[], Awaitable[Any]],
    ) -> asyncio.Task[Any] | None:
        """Inicia sem enfileirar; retorna ``None`` quando todas as vagas estao ocupadas."""
        if len(self._tasks) >= self._max_concurrency:
            return None

        task = asyncio.create_task(coroutine_factory())
        self._tasks.add(task)
        task.add_done_callback(self._finish)
        return task

    def _finish(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        # Recupera eventual excecao da task que terminou depois de o chamador
        # abandonar a espera, evitando warning sem mascarar o erro no await.
        task.exception()


def normalize_text(value: str) -> str:
    """Remove acentos, lowercase, colapsa nao-alfanumerico em espacos."""
    without_accents = "".join(
        ch for ch in unicodedata.normalize("NFKD", value or "")
        if not unicodedata.combining(ch)
    )
    return re.sub(r"[^a-z0-9]+", " ", without_accents.lower()).strip()


def split_message(text: str, limit: int) -> list[str]:
    """Divide mensagem respeitando limites de linha e espaco."""
    if len(text) <= limit:
        return [text]
    parts = []
    while text:
        if len(text) <= limit:
            parts.append(text)
            break
        split_at = text.rfind("\n", 0, limit)
        if split_at <= 0:
            split_at = text.rfind(" ", 0, limit)
        if split_at <= 0:
            split_at = limit
        parts.append(text[:split_at])
        text = text[split_at:].lstrip("\n ")
    return parts


def _is_title_like_numbered_line(body: str) -> bool:
    clean = body.strip().rstrip(":")
    if not clean:
        return False
    if len(clean) > 90:
        return False
    if clean.endswith((".", ";", ",")):
        return False
    if _TITLE_PREFIX_RE.search(clean):
        return True
    # Linhas curtas sem verbo forte costumam ser subtitulos que o modelo numerou.
    return len(clean.split()) <= 6 and not re.match(
        r"^(confirmar|validar|verificar|consultar|executar|acionar|solicitar|"
        r"abrir|reiniciar|configurar|habilitar|desabilitar)\b",
        clean,
        flags=re.IGNORECASE,
    )


def normalize_over_numbered_response(text: str) -> str:
    """Remove numeracao excessiva que o LLM as vezes copia dos documentos.

    Preserva listas numeradas curtas, que normalmente representam passo a passo real.
    Quando a resposta vem com muitos itens numerados, reinicios ou numeros quebrados,
    converte os itens em bullets e promove linhas com cara de secao para subtitulos.
    """
    if not isinstance(text, str) or not text.strip():
        return text

    lines = text.splitlines()
    numbered: list[tuple[int, int, str]] = []
    content_lines = 0
    in_code = False

    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code or not stripped:
            continue
        content_lines += 1
        match = _NUMBERED_LINE_RE.match(line)
        if match:
            numbered.append((idx, int(match.group(2)), match.group(3).strip()))

    if len(numbered) < 5:
        return text

    numbers = [number for _, number, _ in numbered]
    sequence_breaks = sum(
        1
        for previous, current in zip(numbers, numbers[1:])
        if current != previous + 1
    )
    starts_mid_sequence = numbers[0] != 1
    density = len(numbered) / max(content_lines, 1)
    title_like_count = sum(1 for _, _, body in numbered if _is_title_like_numbered_line(body))

    over_numbered = (
        starts_mid_sequence
        or sequence_breaks >= 1
        or (len(numbered) >= 10 and density >= 0.45 and title_like_count >= 1)
    )
    if not over_numbered:
        return text

    normalized: list[str] = []
    in_code = False
    in_sources = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            normalized.append(line)
            continue
        if in_code:
            normalized.append(line)
            continue
        if _SOURCES_HEADING_RE.match(line):
            in_sources = True
            normalized.append(line)
            continue

        match = _NUMBERED_LINE_RE.match(line)
        if not match:
            normalized.append(line)
            continue

        indent, body = match.group(1), match.group(3).strip()
        if not body:
            normalized.append(line)
            continue
        if in_sources:
            normalized.append(f"{indent}- {body}")
        elif _is_title_like_numbered_line(body):
            normalized.append(f"{indent}**{body.rstrip(':')}**")
        else:
            normalized.append(f"{indent}- {body}")

    return "\n".join(normalized)
