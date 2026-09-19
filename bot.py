"""
bot.py — Bot Discord com RAG, replicando o Claude Projects.
"""

import asyncio
import base64
import json
import logging
import re
import time
from pathlib import Path

import discord
from discord.ext import commands

import config
from db import validate_database_config
import rag
from bot_common import (
    ConversationManager,
    InFlightTaskLimiter,
    normalize_over_numbered_response,
    split_message,
)
from ingest import ingest_directory

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ── Bot setup ─────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix=config.COMMAND_PREFIX, intents=intents)

# Historico e cooldown centralizados
_conv = ConversationManager()
_rag_tasks = InFlightTaskLimiter(config.ASK_MAX_CONCURRENCY)


_TABLE_PATTERN = re.compile(
    r"((?:^[ \t]*\|.+\|[ \t]*$\n?){2,})",
    re.MULTILINE,
)


def _discord_conversation_scope(target) -> dict[str, str]:
    """Identifica uma conversa Discord sem tratar o escopo como ACL documental."""
    channel = getattr(target, "channel", target)
    guild = getattr(target, "guild", None) or getattr(channel, "guild", None)
    channel_id = str(getattr(channel, "id", ""))
    parent_id = getattr(channel, "parent_id", None)

    if guild is None:
        return {
            "level": "conversation",
            "platform": "discord",
            "guild_id": "",
            "channel_id": channel_id,
            "thread_id": "",
        }

    return {
        "level": "conversation",
        "platform": "discord",
        "guild_id": str(guild.id),
        "channel_id": str(parent_id or channel_id),
        "thread_id": channel_id if parent_id else "",
    }


def _discord_conversation_key(target) -> tuple[str, ...]:
    scope = _discord_conversation_scope(target)
    return (
        scope["platform"],
        scope["guild_id"],
        scope["channel_id"],
        scope["thread_id"],
    )


def _is_global_feedback_reviewer(user_id: int | str) -> bool:
    return str(user_id) in config.GLOBAL_FEEDBACK_REVIEWER_IDS


def _is_guild_administrator(ctx: commands.Context) -> bool:
    permissions = getattr(getattr(ctx, "author", None), "guild_permissions", None)
    return getattr(ctx, "guild", None) is not None and bool(
        getattr(permissions, "administrator", False)
    )


def _can_review_feedback_scope(ctx: commands.Context, scope: dict | None) -> bool:
    normalized_scope = rag._normalize_scope(scope)
    if normalized_scope.get("level") == "global":
        return _is_global_feedback_reviewer(ctx.author.id)
    if normalized_scope.get("level") != "conversation":
        return _is_global_feedback_reviewer(ctx.author.id)
    return _is_guild_administrator(ctx) and normalized_scope == rag._normalize_scope(
        _discord_conversation_scope(ctx)
    )


def _feedback_submission_scope(target, requested_scope: dict | None) -> dict[str, str]:
    normalized_scope = rag._normalize_scope(requested_scope)
    level = normalized_scope.get("level")
    if not level or level == "conversation":
        return _discord_conversation_scope(target)
    if level == "global":
        return {"level": "global"}
    if level not in {"tenant", "erp", "version"}:
        raise ValueError(
            "Escopo invalido. Use conversation, global, tenant, erp ou version."
        )
    return normalized_scope


def _markdown_table_to_codeblock(text: str) -> str:
    """Converte tabelas Markdown (| col | col |) em blocos de codigo monospace alinhados.

    O Discord nao renderiza tabelas Markdown, entao esta funcao detecta e converte
    automaticamente para um formato legivel com colunas alinhadas.
    """
    # Fast path: sem pipe, sem tabela possivel
    if "|" not in text:
        return text

    def _convert_table(match: re.Match) -> str:
        table_text = match.group(1).strip()
        lines = table_text.split("\n")

        # Parsear celulas de cada linha, ignorando linhas separadoras (|---|---|)
        rows: list[list[str]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            # Detectar e pular linhas separadoras como |---|---| ou | :---: | --- |
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(re.match(r"^:?-+:?$", c) for c in cells if c):
                continue
            rows.append(cells)

        if not rows:
            return table_text

        # Calcular largura maxima de cada coluna
        num_cols = max(len(row) for row in rows)
        col_widths = [0] * num_cols
        for row in rows:
            for i, cell in enumerate(row):
                if i < num_cols:
                    col_widths[i] = max(col_widths[i], len(cell))

        # Formatar linhas com colunas alinhadas
        formatted_lines = []
        for row_idx, row in enumerate(rows):
            parts = []
            for i in range(num_cols):
                cell = row[i] if i < len(row) else ""
                parts.append(cell.ljust(col_widths[i]))
            formatted_lines.append("  ".join(parts))
            # Adicionar separador abaixo do header (primeira linha)
            if row_idx == 0:
                sep_parts = ["-" * w for w in col_widths]
                formatted_lines.append("  ".join(sep_parts))

        return "```\n" + "\n".join(formatted_lines) + "\n```"

    return _TABLE_PATTERN.sub(_convert_table, text)


async def send_split_response(target, response: str, is_reply: bool = True):
    """Envia resposta dividida em partes se necessario."""
    channel = target.channel if hasattr(target, "channel") else target

    # Hardening: garante texto valido para evitar falha silenciosa no envio.
    if isinstance(response, str):
        safe_response = response
    elif response is None:
        safe_response = ""
    else:
        safe_response = str(response)

    if not safe_response.strip():
        safe_response = "Nao foi possivel gerar uma resposta agora. Tente novamente em instantes."

    parts = split_message(safe_response, limit=config.DISCORD_MSG_LIMIT)
    logger.info("Enviando resposta em %d parte(s), total_chars=%d", len(parts), len(safe_response))
    for i, part in enumerate(parts):
        try:
            if i == 0 and is_reply and hasattr(target, "reply"):
                try:
                    await target.reply(part)
                    continue
                except Exception as reply_error:
                    logger.warning("Falha no reply(); tentando channel.send(): %s", reply_error)

            await channel.send(part)
        except Exception as e:
            logger.error("Erro ao enviar mensagem (parte %d/%d): %s", i + 1, len(parts), e)


def _parse_feedback_command_payload(payload: str) -> dict:
    """
    Formato esperado:
      pergunta || resposta_bot || resposta_corrigida || key=value;key=value || tag1,tag2
    Apenas os 3 primeiros campos sao obrigatorios.
    """
    parts = [p.strip() for p in payload.split("||")]
    if len(parts) < 3:
        raise ValueError(
            "Formato invalido. Use: pergunta || resposta_bot || resposta_corrigida "
            "[|| level=conversation|global|tenant;tenant=...;erp=...;version=...] "
            "[|| tag1,tag2]"
        )

    question = parts[0]
    bot_answer = parts[1]
    corrected_answer = parts[2]
    scope: dict[str, str] = {}
    tags: list[str] = []

    if len(parts) >= 4 and parts[3]:
        for item in parts[3].split(";"):
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            key = key.strip().lower()
            value = value.strip()
            if key in {"level", "tenant", "erp", "version"} and value:
                scope[key] = value

    if len(parts) >= 5 and parts[4]:
        tags = [t.strip() for t in parts[4].split(",") if t.strip()]

    return {
        "question": question,
        "bot_answer": bot_answer,
        "corrected_answer": corrected_answer,
        "scope": scope,
        "tags": tags,
    }


# ── Eventos ───────────────────────────────────────────────

@bot.event
async def on_ready():
    logger.info("Bot conectado como %s (ID: %s)", bot.user, bot.user.id)
    logger.info("Prefixo: %s", config.COMMAND_PREFIX)
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.listening,
            name=f"{config.COMMAND_PREFIX}perguntar",
        )
    )


# ── Boas-vindas ──────────────────────────────────────────

@bot.event
async def on_guild_join(guild: discord.Guild):
    """Mensagem de boas-vindas quando o bot e adicionado a um servidor."""
    # Tenta enviar no primeiro canal de texto disponivel
    channel = guild.system_channel or next(
        (ch for ch in guild.text_channels if ch.permissions_for(guild.me).send_messages),
        None,
    )
    if channel:
        embed = discord.Embed(
            title="Ola! Sou o Sabidao :wave:",
            description=(
                "Assistente tecnico da Maxima Sistemas. "
                "Consulto a base de documentos para responder perguntas sobre "
                "maxPedido, ERPs e ferramentas de gestao.\n\n"
                f"Use `{config.COMMAND_PREFIX}perguntar <sua pergunta>` ou me mencione diretamente.\n"
                f"Digite `{config.COMMAND_PREFIX}ajuda` para ver todos os comandos."
            ),
            color=discord.Color.gold(),
        )
        embed.set_footer(text="Dica: Anexe screenshots para analise de erros!")
        try:
            await channel.send(embed=embed)
        except discord.HTTPException:
            pass


# ── Imagens ──────────────────────────────────────────────

_EXT_TO_MEDIA_TYPE = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _get_image_media_type(attachment: discord.Attachment) -> str | None:
    """Detecta media type pela extensao do arquivo (mais confiavel que content_type do Discord)."""
    suffix = Path((attachment.filename or "").lower()).suffix
    media_type = _EXT_TO_MEDIA_TYPE.get(suffix)
    if media_type:
        return media_type
    # Fallback: tentar content_type do Discord (pode vir com charset)
    ct = attachment.content_type or ""
    base_ct = ct.split(";")[0].strip().lower()
    if base_ct.startswith("image/"):
        return base_ct
    return None


async def extract_images(message: discord.Message) -> list[dict]:
    """Extrai imagens dos attachments de uma mensagem Discord."""
    logger.info("extract_images: %d attachments na mensagem", len(message.attachments))
    images = []
    for att in message.attachments:
        logger.info("Attachment recebido: content_type=%s size=%d", att.content_type, att.size)
        media_type = _get_image_media_type(att)
        if not media_type:
            logger.debug("Attachment ignorado (nao e imagem: %s)", att.content_type)
            continue
        if att.size > config.MAX_IMAGE_SIZE_BYTES:
            logger.warning("Imagem ignorada por tamanho (%.1f MB > 20 MB)", att.size / 1024 / 1024)
            continue
        if len(images) >= config.MAX_IMAGES_PER_MESSAGE:
            break
        try:
            img_bytes = await att.read()
            images.append({
                "data": base64.standard_b64encode(img_bytes).decode("utf-8"),
                "media_type": media_type,
            })
            logger.info("Imagem capturada (%s, %.1f KB)", media_type, att.size / 1024)
        except Exception as e:
            logger.error("Erro ao ler imagem (%s).", type(e).__name__)
    return images


# ── Handler de perguntas (compartilhado) ─────────────────

async def handle_question(target, user_id: int, question: str, images: list[dict] = None):
    """Logica compartilhada entre comando e mencao."""
    # Validacao de tamanho
    if len(question) > config.MAX_QUESTION_LENGTH:
        await target.reply(
            f"Pergunta muito longa ({len(question)} caracteres). "
            f"O limite e {config.MAX_QUESTION_LENGTH} caracteres. Tente resumir."
        )
        return

    # Cooldown
    remaining = _conv.check_cooldown(user_id)
    if remaining is not None:
        await target.reply(f"Aguarde {remaining:.0f}s antes de perguntar novamente.")
        return

    conversation_key = _discord_conversation_key(target)
    conversation_scope = _discord_conversation_scope(target)
    async with _conv.serialized(conversation_key):
        await _handle_serialized_question(
            target,
            conversation_key,
            conversation_scope,
            question,
            images,
        )


async def _handle_serialized_question(
    target,
    conversation_key: tuple[str, ...],
    conversation_scope: dict[str, str],
    question: str,
    images: list[dict] | None,
) -> None:
    logger.info("QUERY len=%d images=%d", len(question), len(images or []))

    channel = target.channel if hasattr(target, "channel") else target
    async with channel.typing():
        history = _conv.get_history_snapshot(conversation_key)

        t_start = time.monotonic()
        deadline = t_start + config.ASK_TIMEOUT_SECONDS
        worker = _rag_tasks.try_start(
            lambda: asyncio.to_thread(
                rag.ask,
                question,
                list(history),
                images,
                None,
                "discord",
                conversation_scope,
                deadline=deadline,
            )
        )
        if worker is None:
            await target.reply(
                "O bot esta processando o limite de consultas simultaneas. "
                "Tente novamente em instantes."
            )
            return

        try:
            answer, chunks, trace = await asyncio.wait_for(
                asyncio.shield(worker),
                timeout=max(0.001, deadline - time.monotonic()),
            )
        except (asyncio.TimeoutError, rag.RequestDeadlineExceeded):
            await target.reply(
                "A consulta excedeu o tempo limite. O trabalho ja aceito pode terminar "
                "em segundo plano; tente novamente mais tarde com uma pergunta mais curta "
                "ou especifica."
            )
            return
        except Exception as e:
            logger.error("Erro no RAG (%s).", type(e).__name__)
            await target.reply(f"Erro ao processar a pergunta: {str(e)[:200]}")
            return
        elapsed = time.monotonic() - t_start

        if isinstance(answer, str):
            answer_text = answer
        else:
            logger.warning("Resposta do RAG nao-string (%s). Convertendo para texto.", type(answer).__name__)
            answer_text = str(answer)

        # Atualizar historico (sem imagens para nao estourar memoria)
        _conv.append_exchange(conversation_key, question, answer_text)

        logger.info(
            "ANSWER_TRACE %s",
            json.dumps(
                {
                    "platform": "discord",
                    "elapsed_ms": int(elapsed * 1000),
                    "trace": trace,
                },
                ensure_ascii=False,
            ),
        )

        # Registrar knowledge gap se similaridade baixa
        max_sim = rag._safe_similarity(trace.get("top_similarity", 0.0))
        if max_sim < config.CONFIDENCE_THRESHOLD:
            asyncio.get_running_loop().run_in_executor(
                None, rag.log_knowledge_gap, question, max_sim, "discord"
            )

        # Formatar e enviar (normalizar listas numeradas excessivas e tabelas Markdown)
        response = _markdown_table_to_codeblock(normalize_over_numbered_response(answer_text))
        await send_split_response(target, response)


# ── Comando: perguntar ────────────────────────────────────

@bot.command(name="perguntar", aliases=["ask", "p"])
async def cmd_perguntar(ctx: commands.Context, *, question: str):
    """Faz uma pergunta consultando a base de documentos. Anexe imagens para analise."""
    images = await extract_images(ctx.message)
    await handle_question(ctx, ctx.author.id, question, images)


# ── Comando: status ───────────────────────────────────────

@bot.command(name="status")
async def cmd_status(ctx: commands.Context):
    """Mostra estatisticas da base de conhecimento."""
    async with ctx.typing():
        try:
            stats = rag.get_stats()
            model_info = rag.get_model_config()
            embed = discord.Embed(
                title="Status da Base de Conhecimento",
                color=discord.Color.blue(),
            )
            embed.add_field(
                name="Documentos", value=str(stats.get("total_documents", 0)), inline=True
            )
            embed.add_field(
                name="Chunks indexados", value=str(stats.get("total_chunks", 0)), inline=True
            )
            embed.add_field(
                name="Modelo",
                value=f"{model_info.get('generation_model')} ({model_info.get('llm_provider')})",
                inline=False,
            )
            embed.add_field(
                name="Embeddings",
                value=f"{model_info.get('embedding_model')} ({model_info.get('embedding_provider')})",
                inline=False,
            )
            await ctx.reply(embed=embed)
        except Exception as e:
            await ctx.reply(f"Erro ao buscar status: {e}")


# ── Comando: fontes ───────────────────────────────────────

@bot.command(name="fontes", aliases=["docs", "documentos"])
async def cmd_fontes(ctx: commands.Context):
    """Lista os documentos disponiveis na base."""
    async with ctx.typing():
        try:
            docs = rag.list_documents()
            if not docs:
                await ctx.reply("Nenhum documento indexado ainda. Use `ingest.py` para adicionar documentos.")
                return

            embed = discord.Embed(
                title="Documentos na Base",
                color=discord.Color.green(),
            )
            for doc in docs[:25]:  # Discord limita a 25 fields
                name = doc.get("filename", "?")
                chunks = doc.get("chunk_count", 0)
                doc_type = doc.get("doc_type", "?")
                embed.add_field(
                    name=name,
                    value=f"Tipo: `{doc_type}` | Chunks: `{chunks}`",
                    inline=False,
                )

            await ctx.reply(embed=embed)
        except Exception as e:
            await ctx.reply(f"Erro ao listar documentos: {e}")


# ── Comando: ingerir (admin) ──────────────────────────────

@bot.command(name="ingerir", aliases=["reindex"])
@commands.has_permissions(administrator=True)
async def cmd_ingerir(ctx: commands.Context):
    """Re-processa todos os documentos da pasta. (Admin apenas)"""
    await ctx.reply("Iniciando re-ingestao de documentos... isso pode demorar.")

    async with ctx.typing():
        try:
            results = await asyncio.to_thread(ingest_directory, force=True)
            total = sum(r.get("chunks_count", 0) for r in results)
            success = sum(1 for r in results if r.get("chunks_count", 0) > 0)
            await ctx.reply(
                f"Ingestao concluida!\n"
                f"{success}/{len(results)} documentos processados\n"
                f"{total} chunks indexados no total"
            )
        except Exception as e:
            await ctx.reply(f"Erro na ingestao: {e}")


# ── Comando: limpar (historico) ───────────────────────────

@bot.command(name="limpar", aliases=["clear"])
async def cmd_limpar(ctx: commands.Context):
    """Limpa o historico da conversa (canal, thread ou DM)."""
    conversation_key = _discord_conversation_key(ctx)
    async with _conv.serialized(conversation_key):
        _conv.clear_history(conversation_key)
    await ctx.reply("Historico de conversa limpo!")


# ── Comando: lacunas (admin) ─────────────────────────────

@bot.command(name="lacunas", aliases=["gaps"])
@commands.has_permissions(administrator=True)
async def cmd_lacunas(ctx: commands.Context):
    """Mostra as perguntas mais frequentes sem resposta na base. (Admin apenas)"""
    async with ctx.typing():
        try:
            gaps = await asyncio.to_thread(rag.get_top_knowledge_gaps, 10)
            if not gaps:
                await ctx.reply("Nenhuma lacuna registrada. A base esta cobrindo bem as perguntas!")
                return

            embed = discord.Embed(
                title="Lacunas na Base de Conhecimento",
                description="Perguntas frequentes sem resposta adequada:",
                color=discord.Color.orange(),
            )
            for i, gap in enumerate(gaps[:10], 1):
                query = gap.get("query", "?")[:100]
                count = gap.get("occurrences", 0)
                sim = gap.get("max_similarity", 0)
                embed.add_field(
                    name=f"{i}. ({count}x) sim={sim:.0%}",
                    value=query,
                    inline=False,
                )
            await ctx.reply(embed=embed)
        except Exception as e:
            await ctx.reply(f"Erro ao buscar lacunas: {e}")


# ── Comando: ajuda ───────────────────────────────────────



# -- Comandos: correcoes (review queue) ----------------------------------------

@bot.command(name="corrigir")
async def cmd_corrigir(ctx: commands.Context, *, payload: str):
    """
    Registra uma correcao para revisao.
    Formato:
      !corrigir pergunta || resposta_bot || resposta_corrigida || level=conversation|global || tag1,tag2
    """
    try:
        parsed = _parse_feedback_command_payload(payload)
        feedback_scope = _feedback_submission_scope(ctx, parsed["scope"])
        feedback_id = await asyncio.to_thread(
            rag.submit_feedback_item,
            query=parsed["question"],
            bot_answer=parsed["bot_answer"],
            corrected_answer=parsed["corrected_answer"],
            tags=parsed["tags"],
            scope=feedback_scope,
            created_by=f"discord:{ctx.author.id}",
            platform="discord",
            source_message_id=str(ctx.message.id),
            metadata={"discord_conversation_scope": _discord_conversation_scope(ctx)},
        )
        await ctx.reply(
            f"Correcao registrada com sucesso. ID: `{feedback_id}` (status: `PENDING`)."
        )
    except Exception as e:
        await ctx.reply(f"Erro ao registrar correcao: {e}")


@bot.command(name="correcoes", aliases=["correcoes_pendentes", "correcoes-pendentes"])
async def cmd_correcoes(ctx: commands.Context, action: str = "pendentes", limit: int = 10):
    """Lista correcoes pendentes para revisao."""
    normalized_action = action.lower()
    if normalized_action in {"globais", "global"}:
        if not _is_global_feedback_reviewer(ctx.author.id):
            await ctx.reply("Voce nao tem permissao global para revisar correcoes.")
            return
        review_scope = {"level": "global"}
    elif normalized_action in {"pendentes", "pending", "locais", "local"}:
        if _is_guild_administrator(ctx):
            review_scope = _discord_conversation_scope(ctx)
        elif _is_global_feedback_reviewer(ctx.author.id):
            review_scope = {"level": "global"}
        else:
            await ctx.reply("Voce nao tem permissao para revisar correcoes neste escopo.")
            return
    else:
        await ctx.reply("Uso: `!correcoes pendentes [limite]` ou `!correcoes globais [limite]`")
        return
    async with ctx.typing():
        try:
            pending = await asyncio.to_thread(
                rag.list_pending_feedback_items,
                limit,
                scope=review_scope,
            )
            if not pending:
                await ctx.reply("Nao ha correcoes pendentes.")
                return
            embed = discord.Embed(
                title="Correcoes Pendentes",
                description="Fila de revisao para memoria de correcoes:",
                color=discord.Color.blurple(),
            )
            for item in pending[:25]:
                feedback_id = item.get("id", "?")
                question = (item.get("query") or "?")[:120]
                scope = item.get("scope") or {}
                if isinstance(scope, dict):
                    scope_txt = ", ".join(
                        f"{k}={v}" for k, v in scope.items() if str(v).strip()
                    ) or "global"
                else:
                    scope_txt = "global"
                embed.add_field(
                    name=f"{feedback_id}",
                    value=f"**Pergunta:** {question}\n**Escopo:** {scope_txt}",
                    inline=False,
                )
            await ctx.reply(embed=embed)
        except Exception as e:
            await ctx.reply(f"Erro ao listar correcoes pendentes: {e}")


@bot.command(name="aprovar_correcao", aliases=["aprovar-correcao"])
async def cmd_aprovar_correcao(ctx: commands.Context, feedback_id: str, *, note: str = ""):
    """Aprova uma correcao pendente."""
    try:
        item = await asyncio.to_thread(rag.get_feedback_item, feedback_id)
        if not _can_review_feedback_scope(ctx, item.get("scope")):
            await ctx.reply("Voce nao tem permissao para aprovar correcoes neste escopo.")
            return
        await asyncio.to_thread(
            rag.approve_feedback_item,
            feedback_id,
            f"discord:{ctx.author.id}",
            note or None,
        )
        await ctx.reply(f"Correcao `{feedback_id}` aprovada.")
    except Exception as e:
        await ctx.reply(f"Erro ao aprovar correcao: {e}")


@bot.command(name="rejeitar_correcao", aliases=["rejeitar-correcao"])
async def cmd_rejeitar_correcao(ctx: commands.Context, feedback_id: str, *, note: str = ""):
    """Rejeita uma correcao pendente."""
    try:
        item = await asyncio.to_thread(rag.get_feedback_item, feedback_id)
        if not _can_review_feedback_scope(ctx, item.get("scope")):
            await ctx.reply("Voce nao tem permissao para rejeitar correcoes neste escopo.")
            return
        await asyncio.to_thread(
            rag.reject_feedback_item,
            feedback_id,
            f"discord:{ctx.author.id}",
            note or None,
        )
        await ctx.reply(f"Correcao `{feedback_id}` rejeitada.")
    except Exception as e:
        await ctx.reply(f"Erro ao rejeitar correcao: {e}")


@bot.command(name="publicar_correcao", aliases=["publicar-correcao"])
async def cmd_publicar_correcao(ctx: commands.Context, feedback_id: str):
    """Publica correcao aprovada na memoria vetorial."""
    try:
        item = await asyncio.to_thread(rag.get_feedback_item, feedback_id)
        if not _can_review_feedback_scope(ctx, item.get("scope")):
            await ctx.reply("Voce nao tem permissao para publicar correcoes neste escopo.")
            return
        chunk_id = await asyncio.to_thread(
            rag.publish_feedback_item,
            feedback_id,
            f"discord:{ctx.author.id}",
            None,
        )
        await ctx.reply(
            f"Correcao `{feedback_id}` publicada na memoria vetorial. Chunk: `{chunk_id}`."
        )
    except Exception as e:
        await ctx.reply(f"Erro ao publicar correcao: {e}")

@bot.command(name="ajuda", aliases=["h"])
async def cmd_ajuda(ctx: commands.Context):
    """Mostra os comandos disponiveis e como usar o bot."""
    embed = discord.Embed(
        title="Sabidao — Assistente Tecnico Maxima",
        description=(
            "Consulto a base de documentos da Maxima para responder perguntas tecnicas "
            "sobre maxPedido, ERPs e ferramentas de gestao."
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name=f"`{config.COMMAND_PREFIX}perguntar <pergunta>`",
        value="Faz uma pergunta consultando a base. Aceita imagens em anexo.\nAtalhos: `!ask`, `!p`",
        inline=False,
    )
    embed.add_field(
        name=f"`@{config.BOT_NAME} <pergunta>`",
        value="Mencione o bot diretamente para perguntar sem comando.",
        inline=False,
    )
    embed.add_field(
        name=f"`{config.COMMAND_PREFIX}status`",
        value="Mostra estatisticas da base (documentos, chunks, modelo).",
        inline=False,
    )
    embed.add_field(
        name=f"`{config.COMMAND_PREFIX}fontes`",
        value="Lista os documentos indexados na base.",
        inline=False,
    )
    embed.add_field(
        name=f"`{config.COMMAND_PREFIX}limpar`",
        value="Limpa o historico de conversa deste canal.",
        inline=False,
    )
    embed.add_field(
        name=f"`{config.COMMAND_PREFIX}ping`",
        value="Verifica se o bot esta online.",
        inline=False,
    )
    embed.add_field(
        name=f"`{config.COMMAND_PREFIX}corrigir <payload>`",
        value=(
            "Registra correcao para revisao. Formato: "
            "`pergunta || resposta_bot || resposta_corrigida || level=...;tenant=... || tag1,tag2`. "
            "Sem `level`, usa a conversa atual."
        ),
        inline=False,
    )
    embed.add_field(
        name=f"`{config.COMMAND_PREFIX}correcoes pendentes`",
        value="(Admin local ou revisor global configurado) Lista a fila do escopo autorizado.",
        inline=False,
    )
    embed.add_field(
        name=f"`{config.COMMAND_PREFIX}aprovar-correcao <id>` / `{config.COMMAND_PREFIX}rejeitar-correcao <id>` / `{config.COMMAND_PREFIX}publicar-correcao <id>`",
        value=(
            "Admins revisam apenas a conversa atual; correcoes globais exigem um "
            "revisor configurado explicitamente."
        ),
        inline=False,
    )
    embed.set_footer(text="Dica: Anexe screenshots para analise de erros e telas do sistema.")
    await ctx.reply(embed=embed)


# ── Comando: ping ────────────────────────────────────────

@bot.command(name="ping")
async def cmd_ping(ctx: commands.Context):
    """Verifica se o bot esta online e a latencia."""
    await ctx.reply(f"Pong! Latencia: {bot.latency * 1000:.0f}ms")


# ── Responder mencoes ─────────────────────────────────────

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return

    await bot.process_commands(message)

    if bot.user in message.mentions and not message.content.startswith(config.COMMAND_PREFIX):
        question = message.content.replace(f"<@{bot.user.id}>", "").replace(f"<@!{bot.user.id}>", "").strip()
        images = await extract_images(message)

        if not question and not images:
            await message.reply(
                f"Me faca uma pergunta ou use `{config.COMMAND_PREFIX}perguntar <sua pergunta>`"
            )
            return

        if not question and images:
            question = "Analise esta imagem e descreva o que voce ve. Se for um erro ou tela do sistema, explique o que esta acontecendo."

        await handle_question(message, message.author.id, question, images)


# ── Error handling ────────────────────────────────────────

@bot.event
async def on_command_error(ctx: commands.Context, error):
    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(f"Argumento faltando. Uso: `{config.COMMAND_PREFIX}perguntar <sua pergunta>`")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.reply("Voce nao tem permissao para usar este comando.")
    else:
        logger.error("Erro no comando: %s", error)
        await ctx.reply(f"Ocorreu um erro: {str(error)[:200]}")


# ── Start ─────────────────────────────────────────────────

if __name__ == "__main__":
    config.validate()
    validate_database_config()
    logger.info("Iniciando bot...")
    bot.run(config.DISCORD_TOKEN)
