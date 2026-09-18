# Relatório de Reversão: Gemini → Claude (Anthropic)

> **Data da migração Gemini:** 2026-03-12
> **Motivo:** API Claude sem tokens. Usando Gemini temporariamente.
> **Objetivo deste documento:** Reverter TODAS as mudanças e voltar ao Claude exatamente como estava.

---

## Resumo das Mudanças

| Arquivo | O que mudou |
|---------|------------|
| `rag.py` | Removido `import anthropic`, adicionado `google.genai`. Criadas funções `_gemini_generate()` e `_anthropic_msgs_to_gemini()`. 3 chamadas LLM convertidas. Error handling reescrito. |
| `config.py` | Removido `ANTHROPIC_API_KEY` e `CLAUDE_MODEL`. Adicionado `GEMINI_MODEL`. Defaults de modelos auxiliares trocados. |
| `.env` | Removida seção Anthropic. Adicionado `GEMINI_MODEL`. System prompt expandido. |
| `.env.example` | Mesmo que `.env` (template). |
| `ingest.py` | Contextual Retrieval usa `_gemini_generate` em vez de `get_anthropic`. |
| `bot.py` | `config.CLAUDE_MODEL` → `config.GEMINI_MODEL` (linha 294). |
| `requirements.txt` | Removido `anthropic>=0.42.0`. |

---

## 1. `requirements.txt` — Adicionar anthropic de volta

**Adicionar esta linha:**
```
anthropic>=0.42.0
```

Depois rodar:
```bash
pip install anthropic>=0.42.0
```

---

## 2. `.env` — Restaurar seção Anthropic

**Adicionar ANTES da seção Google Gemini:**
```env
# ==========================================
# Anthropic Claude — LLM principal
# ==========================================
ANTHROPIC_API_KEY=sua_chave_anthropic_aqui
CLAUDE_MODEL=claude-sonnet-4-20250514
```

**Remover esta linha:**
```env
GEMINI_MODEL=gemini-2.5-pro
```

**Trocar modelos auxiliares de volta (ou manter gemini-2.5-flash se preferir economizar Claude):**
```env
REFORMULATION_MODEL=claude-haiku-3-5-20241022
CONTEXTUAL_RETRIEVAL_MODEL=claude-haiku-4-5-20251001
```

> **Nota:** Os modelos auxiliares (reformulação, reranking, contextual retrieval) podem continuar usando `gemini-2.5-flash` se quiser economizar tokens do Claude. Só o modelo PRINCIPAL precisa ser Claude.

---

## 3. `.env.example` — Atualizar template

Mesmas mudanças do `.env` acima, mas com valores placeholder.

---

## 4. `config.py` — Restaurar variáveis Claude

### 4.1. Adicionar de volta (após linha do `GEMINI_API_KEY`):
```python
# Anthropic Claude (LLM principal)
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")
```

### 4.2. Remover:
```python
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-pro")
```

### 4.3. Trocar defaults dos modelos auxiliares:
```python
# DE:
REFORMULATION_MODEL = os.getenv("REFORMULATION_MODEL", "gemini-2.5-flash")
CONTEXTUAL_RETRIEVAL_MODEL = os.getenv("CONTEXTUAL_RETRIEVAL_MODEL", "gemini-2.5-flash")

# PARA:
REFORMULATION_MODEL = os.getenv("REFORMULATION_MODEL", "claude-haiku-3-5-20241022")
CONTEXTUAL_RETRIEVAL_MODEL = os.getenv("CONTEXTUAL_RETRIEVAL_MODEL", "claude-haiku-4-5-20251001")
```

### 4.4. Adicionar `ANTHROPIC_API_KEY` à validação:
No dict `_REQUIRED_SHARED` (por volta da linha 186), adicionar:
```python
"ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
```

---

## 5. `rag.py` — O ARQUIVO PRINCIPAL (maior impacto)

### 5.1. Imports — Restaurar anthropic, remover extras

**REMOVER estas linhas (linhas 8, 22):**
```python
import base64 as _base64
from google.genai import types as _gtypes
```

**ADICIONAR:**
```python
import anthropic
```

> **MANTER** `from google import genai` — ainda é usado para embeddings.

### 5.2. Singleton do cliente Anthropic — Restaurar

**REMOVER** o singleton `_gemini` e `get_gemini()` (linhas 208, 212-216):
```python
# REMOVER:
_gemini: Optional[genai.Client] = None

def get_gemini() -> genai.Client:
    global _gemini
    if _gemini is None:
        _gemini = genai.Client(api_key=config.GEMINI_API_KEY)
    return _gemini
```

**SUBSTITUIR POR:**
```python
_anthropic: Optional[anthropic.Anthropic] = None

def get_anthropic() -> anthropic.Anthropic:
    global _anthropic
    if _anthropic is None:
        _anthropic = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _anthropic
```

> **IMPORTANTE:** Criar um `get_gemini()` SEPARADO só para embeddings:
```python
_gemini_embed: Optional[genai.Client] = None

def get_gemini() -> genai.Client:
    global _gemini_embed
    if _gemini_embed is None:
        _gemini_embed = genai.Client(api_key=config.GEMINI_API_KEY)
    return _gemini_embed
```

### 5.3. REMOVER funções Gemini-only (linhas 219-265)

**REMOVER INTEIRAMENTE:**
```python
def _gemini_generate(...):
    ...

def _anthropic_msgs_to_gemini(...):
    ...
```

### 5.4. `_reformulate_query_with_history()` — Voltar para Anthropic

**TROCAR (por volta da linha 1075-1090):**
```python
# ESTÁ ASSIM (Gemini):
response = _gemini_generate(
    model=config.REFORMULATION_MODEL,
    max_tokens=200,
    system=("Voce reescreve perguntas..."),
    contents=("Historico recente:..."),
)
reformulated = response.text.strip()
```

**VOLTAR PARA:**
```python
response = get_anthropic().messages.create(
    model=config.REFORMULATION_MODEL,
    max_tokens=200,
    system="Voce reescreve perguntas de follow-up para serem autocontidas. "
           "Substitua pronomes e referencias vagas pelo tema correto do historico. "
           "Retorne APENAS a pergunta reescrita, sem explicacao. "
           "Se a pergunta ja for autocontida, retorne-a como esta.",
    messages=[{
        "role": "user",
        "content": (
            f"Historico recente:\n{history_text}\n\n"
            f"Pergunta atual: {question}\n\n"
            "Reescreva a pergunta para ser autocontida:"
        ),
    }],
)
reformulated = response.content[0].text.strip()
```

### 5.5. `_rerank_chunks_with_llm()` — Voltar para Anthropic

**TROCAR (por volta da linha 1136-1147):**
```python
# ESTÁ ASSIM (Gemini):
response = _gemini_generate(
    model=config.REFORMULATION_MODEL,
    max_tokens=200,
    system=("Voce e um ranqueador..."),
    contents=f"Pergunta: {query}\n\nTrechos:\n{summaries_text}",
)
ranking_text = response.text.strip()
```

**VOLTAR PARA:**
```python
response = get_anthropic().messages.create(
    model=config.REFORMULATION_MODEL,
    max_tokens=200,
    system="Voce e um ranqueador de documentos tecnicos. "
           "Dada uma pergunta e trechos de documentos, retorne os indices dos trechos "
           "mais relevantes para responder a pergunta, do mais relevante ao menos. "
           "Retorne APENAS os numeros separados por virgula. Exemplo: 3,0,7,1",
    messages=[{
        "role": "user",
        "content": f"Pergunta: {query}\n\nTrechos:\n{summaries_text}",
    }],
)
ranking_text = response.content[0].text.strip()
```

### 5.6. `ask()` — A MUDANÇA PRINCIPAL (linhas 1258-1305)

**REMOVER todo o bloco Gemini (linhas 1258-1305):**
```python
# REMOVER TUDO ISSO:
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

try:
    response = _gemini_generate(
        model=config.GEMINI_MODEL,
        max_tokens=config.ASK_MAX_TOKENS,
        system=system,
        contents=gemini_contents,
    )
    if response.text:
        answer = response.text
    else:
        ...
except Exception as e:
    error_str = str(e).lower()
    if "429" in str(e) or "resource_exhausted" in error_str ...
    ...
```

**SUBSTITUIR POR (formato Anthropic original):**
```python
    # Montar conteudo da mensagem (texto + imagens)
    user_content = []
    if images:
        for img in images:
            user_content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img["media_type"],
                    "data": img["data"],
                },
            })
    user_content.append({"type": "text", "text": question})

    # Montar mensagens
    messages = []
    if conversation_history:
        messages.extend(conversation_history)
    messages.append({"role": "user", "content": user_content})

    # -- Resposta do Claude --
    try:
        response = get_anthropic().messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=config.ASK_MAX_TOKENS,
            system=system,
            messages=messages,
        )
        answer = response.content[0].text
    except anthropic.RateLimitError:
        logger.error("Rate limit do Claude atingido")
        answer = "O servico esta sobrecarregado no momento. Tente novamente em alguns segundos."
    except anthropic.AuthenticationError:
        logger.error("Erro de autenticacao com Claude")
        answer = "Erro de configuracao do bot. Contate o administrador."
    except anthropic.APITimeoutError:
        logger.error("Timeout na chamada ao Claude")
        answer = "A consulta demorou demais. Tente reformular com uma pergunta mais curta."
    except anthropic.APIConnectionError:
        logger.error("Erro de conexao com Claude")
        answer = "Nao foi possivel conectar ao servico. Tente novamente em instantes."
    except Exception as e:
        logger.error("Erro ao chamar Claude: %s", e, exc_info=True)
        answer = "Ocorreu um erro inesperado. Tente novamente."

    return answer, chunks
```

---

## 6. `ingest.py` — Contextual Retrieval

**TROCAR (por volta da linha 814):**
```python
# ESTÁ ASSIM:
from rag import _gemini_generate

response = _gemini_generate(
    model=config.CONTEXTUAL_RETRIEVAL_MODEL,
    max_tokens=max_tokens,
    system=system_prompt,
    contents=user_prompt,
)
raw_text = response.text.strip()
```

**VOLTAR PARA:**
```python
from rag import get_anthropic

response = get_anthropic().messages.create(
    model=config.CONTEXTUAL_RETRIEVAL_MODEL,
    max_tokens=max_tokens,
    system=system_prompt,
    messages=[{"role": "user", "content": user_prompt}],
)
raw_text = response.content[0].text.strip()
```

---

## 7. `bot.py` — Linha 294

**TROCAR:**
```python
embed.add_field(name="Modelo", value=config.GEMINI_MODEL, inline=False)
```

**PARA:**
```python
embed.add_field(name="Modelo", value=config.CLAUDE_MODEL, inline=False)
```

---

## Checklist de Reversão

- [ ] `pip install anthropic>=0.42.0`
- [ ] Adicionar `anthropic>=0.42.0` ao `requirements.txt`
- [ ] Adicionar `ANTHROPIC_API_KEY` e `CLAUDE_MODEL` no `.env`
- [ ] Remover `GEMINI_MODEL` do `.env`
- [ ] Atualizar `.env.example`
- [ ] Atualizar `config.py` (variáveis + validação + defaults)
- [ ] Restaurar `import anthropic` em `rag.py`
- [ ] Remover `import base64 as _base64` e `from google.genai import types as _gtypes` de `rag.py`
- [ ] Restaurar singleton `get_anthropic()` em `rag.py`
- [ ] Remover `_gemini_generate()` e `_anthropic_msgs_to_gemini()` de `rag.py`
- [ ] Reverter `_reformulate_query_with_history()` para Anthropic
- [ ] Reverter `_rerank_chunks_with_llm()` para Anthropic
- [ ] Reverter `ask()` para Anthropic (mensagens + error handling)
- [ ] Reverter contextual retrieval em `ingest.py`
- [ ] Trocar `config.GEMINI_MODEL` → `config.CLAUDE_MODEL` em `bot.py`
- [ ] Testar bot com `py bot.py`

---

## Modelos Claude recomendados na volta

| Uso | Modelo | Custo |
|-----|--------|-------|
| **Resposta principal** | `claude-sonnet-4-20250514` | Médio |
| **Reformulação / Reranking** | `claude-haiku-3-5-20241022` | Baixo |
| **Contextual Retrieval** | `claude-haiku-4-5-20251001` | Baixo |

> Se quiser o melhor modelo possível para respostas: `claude-opus-4-6` (mais caro).
> O Sonnet 4 é o melhor custo-benefício para suporte técnico.

---

## Nota sobre o System Prompt

O system prompt foi melhorado durante a migração (regras anti-verbosidade). Essas melhorias são **independentes do modelo** — manter o prompt atualizado tanto no Gemini quanto no Claude. Não precisa reverter o prompt.
