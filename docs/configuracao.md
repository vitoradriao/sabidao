# Configuração de providers e secrets

[Documentação](README.md) / Configuração

O processo lê primeiro as variáveis já presentes no ambiente. O arquivo `.env`,
quando existe, preenche somente as variáveis ausentes. Assim, secrets injetados
por Docker, CI ou pelo serviço do sistema prevalecem sobre valores locais.

## Geração e embeddings

Geração de texto e embeddings têm configuração independente:

| Finalidade | Provider | Credencial | Endpoint | Modelo |
| --- | --- | --- | --- | --- |
| Respostas | `GENERATION_PROVIDER` | `GENERATION_API_KEY` | `GENERATION_BASE_URL` | `GENERATION_MODEL` |
| Vetores | `EMBEDDING_PROVIDER` | `EMBEDDING_API_KEY` | `EMBEDDING_BASE_URL` | `EMBEDDING_MODEL` |

Os providers aceitos são `gemini` e `openai`. Os endpoints são usados somente
por providers compatíveis com OpenAI. Mesmo quando os dois serviços usam a
mesma chave, defina as duas variáveis de credencial de forma explícita. Alterar
o provider, endpoint ou modelo de geração não modifica a configuração dos
embeddings e não migra os vetores já armazenados.

`GENERATION_MODEL_POLICY` controla explicitamente o modelo usado na resposta:

- `primary` (default) usa `GENERATION_MODEL`, independentemente do tamanho do prompt;
- `contextual` usa o modelo contextual do provider (`CONTEXTUAL_RETRIEVAL_MODEL`
  no Gemini ou `OPENAI_CONTEXTUAL_MODEL` no OpenAI).

Não existe troca automática de modelo por quantidade de caracteres. Um fallback
por resposta vazia continua possível, mas fica registrado como uma chamada
separada no trace.

O espaço vetorial é identificado por `EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`,
`EMBEDDING_DIMENSIONS` e `EMBEDDING_PREPROCESSING_VERSION`. Esta versão aceita
somente 1536 dimensões. Alterar qualquer componente da identidade exige validar
ou reconstruir o índice conforme o guia de
[identidade do índice vetorial](identidade-indice-vetorial.md).

O bot valida providers, credenciais, modelos e URLs antes de iniciar. As
mensagens de erro citam apenas o nome da configuração inválida; secrets não são
incluídos nos avisos ou erros de validação.

## Compatibilidade com nomes antigos

Instalações existentes continuam funcionando com os nomes abaixo enquanto a
migração é feita. Quando um fallback antigo é usado, o processo registra apenas
os nomes das variáveis, nunca seus valores.

| Nome antigo | Substituição |
| --- | --- |
| `LLM_PROVIDER` | `GENERATION_PROVIDER`; no modo legado também preenche `EMBEDDING_PROVIDER` quando ele estiver ausente |
| `GEMINI_API_KEY` ou `OPENAI_API_KEY` | `GENERATION_API_KEY` e `EMBEDDING_API_KEY` |
| `GEMINI_MODEL` ou `OPENAI_MODEL` | `GENERATION_MODEL` |
| `OPENAI_BASE_URL` | `GENERATION_BASE_URL` e `EMBEDDING_BASE_URL` |
| `OPENAI_EMBEDDING_MODEL` | `EMBEDDING_MODEL` |

Se o nome novo e o antigo estiverem definidos, o nome novo prevalece. Depois de
confirmar a configuração efetiva, remova os nomes antigos do ambiente e do
`.env` para eliminar os avisos. Em uma configuração nova, omitir
`EMBEDDING_PROVIDER` seleciona `gemini` e nunca herda
`GENERATION_PROVIDER`; a herança ocorre apenas para preservar um arquivo legado
que ainda usa `LLM_PROVIDER`.

## Defaults internos e perfil de exemplo

Os defaults em `config.py` são valores conservadores para uma variável ausente.
O `.env.example` representa o perfil operacional deste repositório e substitui
intencionalmente alguns deles:

| Variável | Default interno | `.env.example` | Motivo do perfil |
| --- | ---: | ---: | --- |
| `MAX_CONTEXT_CHUNKS` | 8 | 24 | ampliar a cobertura do corpus operacional |
| `SIMILARITY_THRESHOLD` | 0,55 | 0,40 | recuperar mais candidatos para as etapas de filtro |
| `RAG_MIN_STRONG_SIMILARITY` | 0,62 | 0,42 | calibrar a abstenção ao corpus atual |
| `RAG_OPERATIONAL_SIMILARITY_MARGIN` | 0,05 | 0,03 | usar margem menor no perfil calibrado |
| `RAG_MAX_REGEN_ATTEMPTS` | 1 | 2 | permitir uma correção adicional de grounding |
| `ASK_TIMEOUT_SECONDS` | 120 | 240 | acomodar respostas extensas em produção |
| `ASK_MAX_TOKENS` | 8192 | 16384 | permitir o limite operacional configurado |
| `MAX_HISTORY_PAIRS` | 20 | 12 | limitar o crescimento do contexto por conversa |
| `CONTEXTUAL_RETRIEVAL_MAX_TOKENS` | 150 | 250 | enriquecer chunks com mais contexto no perfil atual |
| `CONTEXTUAL_RETRIEVAL_BATCH_SIZE` | 50 | 20 | reduzir a pressão por lote sobre o provider |

Para conferir a configuração sem expor credenciais, use `!status`: o resumo
mostra providers e modelos ativos, mas não mostra chaves.

## Telemetria de geração e custo estimado

O `ASK_TRACE` registra cada chamada de modelo separadamente com `request_id`,
etapa (`reformulation`, `rerank`, `generation` ou `regeneration`), provider,
modelo efetivo, latência, status, tentativa, `finish_reason` e usage informado
pelo provider. Entrada, cache, saída e reasoning são mantidos em campos
separados; dados ausentes permanecem `null` e não são convertidos em zero.

O custo é uma estimativa em USD com a tabela `2026-09-19` embutida no código.
Ela cobre os modelos padrão do projeto e usa os preços públicos das páginas
oficiais do [GPT-5.4](https://developers.openai.com/api/docs/models/gpt-5.4),
[GPT-5.4 Mini](https://developers.openai.com/api/docs/models/gpt-5.4-mini) e do
[Gemini](https://ai.google.dev/gemini-api/docs/pricing). Modelos ou dados de
usage desconhecidos produzem custo `null`; o trace também informa quanto do
custo conhecido pôde ser agregado. Atualize a versão e os valores da tabela
quando os preços oficiais mudarem. A estimativa usa a tarifa paga padrão para
texto e não inclui free tier, batch, flex, priority, processamento regional,
armazenamento de cache ou ferramentas cobradas separadamente.

## Relaxamento controlado do filtro de módulo

Quando `RAG_FILTER_BY_MODULE=true`, a recuperação mantém a busca roteada como
preferencial e executa também um *challenger* sem o filtro de módulo. Essa
segunda busca evita que uma classificação incorreta torne um documento
canônico inalcançável, mas não desativa os demais filtros nem aumenta o
contexto indefinidamente.

`RAG_ENABLE_GLOBAL_CHALLENGER` ativa o comportamento. O número de resultados e
o teto de candidatos, incluindo vizinhos, são limitados respectivamente por
`RAG_GLOBAL_CHALLENGER_COUNT` e `RAG_GLOBAL_CHALLENGER_FETCH_LIMIT`. A consulta
reaproveita o embedding já calculado, não faz chamada adicional ao modelo de
geração e continua sujeita a `DB_STATEMENT_TIMEOUT_MS` e às tentativas já
definidas para chamadas transientes ao banco.

O trace de cada resposta registra os módulos preferenciais, o motivo do
relaxamento, as quantidades de candidatos filtrados, globais e selecionados,
além da latência observada do *challenger*. Uma afirmação de ausência não deve
ser sustentada apenas pelo subconjunto filtrado; sem evidência suficiente após
o relaxamento, a resposta deve assumir incerteza com a frase padrão de
*no-answer*.
