# Configuração de providers e secrets

[Documentação](README.md) / Configuração

O processo lê primeiro as variáveis já presentes no ambiente. O arquivo `.env`,
quando existe, preenche somente as variáveis ausentes. Assim, secrets injetados
por Docker, CI ou pelo serviço do sistema prevalecem sobre valores locais.

O `.env.example` é apenas um ponto de partida local e contém placeholders, não
credenciais adequadas para implantação. Em ambientes compartilhados ou de
produção, injete `DISCORD_TOKEN`, chaves dos providers e `POSTGRES_PASSWORD`
por um mecanismo protegido. A senha presente em `DATABASE_URL` deve coincidir
com `POSTGRES_PASSWORD` e não deve aparecer em commits, logs ou mensagens do
Discord.

No Compose, o PostgreSQL é acessível entre contêineres por `postgres:5432` e,
para ferramentas no host, fica vinculado por padrão apenas a
`127.0.0.1:${POSTGRES_PORT}`. `POSTGRES_BIND_ADDRESS` existe para configuração
explícita de rede; não use `0.0.0.0` sem necessidade documentada e proteção de
rede apropriada.

## Geração e embeddings

Geração de texto e embeddings têm configuração independente:

| Finalidade | Provider | Credencial | Endpoint | Modelo |
| --- | --- | --- | --- | --- |
| Respostas | `GENERATION_PROVIDER` | `GENERATION_API_KEY` | `GENERATION_BASE_URL` | `GENERATION_MODEL` |
| Vetores | `EMBEDDING_PROVIDER` | `EMBEDDING_API_KEY` | `EMBEDDING_BASE_URL` | `EMBEDDING_MODEL` |

Os providers aceitos são `gemini` e `openai`. Os endpoints são usados somente
por providers compatíveis com OpenAI. Mesmo quando os dois serviços usam a
mesma chave, defina as duas variáveis de credencial de forma explícita. Alterar
o modelo primário de resposta não modifica a configuração dos embeddings nem
migra os vetores já armazenados. Quando a contextualização da ingestão está
ativa, porém, o provider de geração e o modelo contextual efetivamente resolvido
participam do texto enviado ao embedding e da identidade de processamento.

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
| `ASK_MAX_CONCURRENCY` | 4 | 4 | limitar o trabalho simultâneo aceito pelo processo |
| `ASK_MAX_TOKENS` | 8192 | 16384 | permitir o limite operacional configurado |
| `MAX_HISTORY_PAIRS` | 20 | 12 | limitar o crescimento do contexto por conversa |
| `CONTEXTUAL_RETRIEVAL_MAX_TOKENS` | 150 | 250 | enriquecer chunks com mais contexto no perfil atual |
| `CONTEXTUAL_RETRIEVAL_BATCH_SIZE` | 50 | 20 | reduzir a pressão por lote sobre o provider |

Para conferir a configuração sem expor credenciais, use `!status`: o resumo
mostra providers e modelos ativos, mas não mostra chaves.

## Contextualização da ingestão

Com `CONTEXTUAL_RETRIEVAL_ENABLED=true`, a ingestão gera uma frase curta para
situar cada trecho antes de criar seu embedding. O conteúdo original continua
armazenado em `document_chunks.content` e é o único apresentado como evidência;
o texto gerado pelo modelo não vira citação do documento.

A identidade de processamento registra, sem credenciais, o provider e o modelo
contextual efetivamente resolvidos, a versão e o hash do contrato de prompt, além
de `CONTEXTUAL_RETRIEVAL_MAX_DOC_CHARS`,
`CONTEXTUAL_RETRIEVAL_MAX_TOKENS` e `CONTEXTUAL_RETRIEVAL_BATCH_SIZE`. Alterar
qualquer um desses componentes invalida o `processing_hash`; alterar somente o
modelo primário de resposta não invalida a ingestão quando o modelo contextual
permanece igual.

`CONTEXTUAL_RETRIEVAL_MAX_DOC_CHARS` limita a parte do documento reenviada em
cada chamada, e `CONTEXTUAL_RETRIEVAL_MAX_TOKENS` é encaminhado ao provider como
limite de saída. O lote contextual é aplicado sobre todos os trechos preparados,
independentemente de `EMBEDDING_BATCH_SIZE`; em seguida, os textos resultantes são
divididos nos lotes de embeddings.

Esta mudança de contrato não inicia ingestão, reindexação ou chamadas externas
automaticamente. Na próxima execução explícita de `ingest.py` ou `!ingerir`, os
documentos contextualizados sob a identidade antiga serão detectados como
alterados. Planeje essa reingestão, custo e janela operacional antes de executar;
a versão válida do índice é preservada se a preparação falhar.

## Concorrência, deadline e retries

`ASK_MAX_CONCURRENCY` limita quantas perguntas podem manter trabalho ativo no
processo. Quando todas as vagas estão ocupadas, uma nova pergunta é recusada
imediatamente com uma mensagem de ocupação, sem formar fila local. Cada vaga só
é liberada quando a execução real termina, inclusive se o Discord já recebeu a
mensagem de timeout.

`ASK_TIMEOUT_SECONDS` também define o orçamento total propagado pelas etapas do
RAG. O prazo começa na chegada da pergunta ao bot, antes da admissão por
conversa, e inclui qualquer tempo gasto até o início do worker. Uma etapa ou
tentativa nova não começa depois do deadline, e os timeouts das chamadas aos
providers são reduzidos ao tempo restante. O encerramento da espera no Discord
não cancela uma requisição que o provider já tenha aceitado e não garante
interrupção de cobrança ou processamento remoto.

Falhas transitórias de transporte e HTTP 408, 429 ou 5xx elegíveis usam no
máximo `RAG_PROVIDER_MAX_RETRIES` novas tentativas, com espera exponencial a
partir de `RAG_RETRY_BASE_SECONDS` ou o cabeçalho `Retry-After`. A tentativa é
pulada se a espera não couber no deadline. Erros de autenticação e permissão
não são repetidos.

## Conversas e revisão de feedback

O histórico é serializado por conversa. Em servidores, a chave combina
servidor, canal e thread; em mensagens diretas, usa o canal de DM separado. Uma
thread não compartilha histórico nem correções locais com o canal pai. O limite
LRU remove o histórico e o lock juntos somente quando a conversa está inativa.
Perguntas não formam fila por conversa: enquanto uma pergunta ocupa a chave, as
seguintes recebem uma resposta de ocupação imediata. Conversas diferentes
continuam independentes e podem progredir até `ASK_MAX_CONCURRENCY`.

Correções enviadas pelo Discord usam a conversa atual por padrão. A recuperação
combina apenas correções com esse escopo exato e correções explicitamente
globais. Administradores de um servidor podem revisar somente a conversa em
que o comando foi usado. Para revisar, rejeitar ou publicar correções globais,
configure os IDs Discord autorizados em `GLOBAL_FEEDBACK_REVIEWER_IDS`,
separados por vírgula. Permissão administrativa no servidor ou o uso do comando
por DM não concede autorização global.

O escopo da conversa organiza histórico e memória de feedback; ele não é uma
ACL de documentos nem implementa isolamento entre clientes. Uma implantação
com múltiplos tenants precisa de requisitos e controles de acesso próprios.

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
