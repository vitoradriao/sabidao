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

## Cliente TypeSafe/Jev (issue #89)

O cliente JEV-01 está presente na `master` e é opcional: a
política de cada consumidor define quando uma decisão Jev é necessária; não há
modo *shadow* nem uma flag global adicional nesta entrega.

O transporte usa `POST` para o endpoint fixo
`https://api.typesafe.ai/v1/systemone`, com autenticação `Bearer`. O endpoint
não é configurável por dados do usuário ou por variável de ambiente. Importar
ou instanciar o cliente não cria cliente HTTP, worker ou requisição; esses
recursos só são usados quando um consumidor efetivamente solicita uma decisão.

### Configuração opcional

Os valores abaixo são os defaults operacionais da entrega. Eles limitam o
cliente, mas não constituem garantia de latência do provider.

| Variável | Default | Validação e finalidade |
| --- | ---: | --- |
| `TYPESAFE_API_KEY` | ausente | Obrigatória somente quando um consumidor Jev estiver ativo; nunca é registrada. |
| `JEV_MODEL` | `jev-1.13.0` | Identificador Jev versionado no formato `jev-X.Y.Z`; aliases `latest` e `preview` são rejeitados. |
| `JEV_MAX_CONCURRENCY` | `4` | Capacidade global do processo, entre `1` e `8`; não cria fila ilimitada. |
| `JEV_REQUEST_TIMEOUT_SECONDS` | `2.0` | Limite positivo e finito para uma chamada HTTP. |
| `JEV_STAGE_TIMEOUT_SECONDS` | `8.0` | Limite positivo e finito para o orçamento da etapa Jev. |
| `JEV_MIN_REMAINING_SECONDS` | `20.0` | Reserva finita e não negativa que deve permanecer antes de iniciar uma chamada. |

Quando nenhum consumidor usa Jev, a chave pode permanecer ausente e a
configuração não é validada nem produz chamadas. Quando um consumidor o ativa,
a ausência de `TYPESAFE_API_KEY`, um modelo inválido ou um limite numérico
inválido produz erro de configuração claro; a função consumidora deve tratar
esse erro sem fabricar uma decisão semântica. A chave, seu valor e o conteúdo
das requisições não aparecem em logs ou mensagens de erro.

### Admissão, deadline e capacidade

O consumidor fornece o deadline absoluto da solicitação e, quando aplicável,
o orçamento restante da etapa. O timeout efetivo é o menor entre
`JEV_REQUEST_TIMEOUT_SECONDS`, `JEV_STAGE_TIMEOUT_SECONDS` e o tempo restante
até o deadline. Se o deadline já venceu, se o orçamento da etapa acabou ou se
o tempo restante não cobre `JEV_MIN_REMAINING_SECONDS`, a chamada não começa e
o resultado fica em `budget_exhausted`. A reserva preserva o orçamento do
gerador e não deve ser consumida pelo fan-out Jev.

`JEV_MAX_CONCURRENCY` limita os trabalhos ativos em um executor compartilhado
por todo o processo, inclusive entre perguntas diferentes. Sem slot livre, o
cliente retorna imediatamente `capacity_exhausted`; não aguarda nem acumula
trabalho em uma fila local ilimitada. Um candidato adicional não é um retry:
cada decisão mantém `attempt=1`.

O timeout da espera não cancela uma thread que já começou. O slot permanece
ocupado até o trabalho subjacente terminar, mesmo que o chamador já tenha
recebido `timeout`. Também não há garantia de cancelamento do trabalho que o
provider já aceitou nem de interrupção de seu processamento remoto ou cobrança.

### Retries e estados de erro

O cliente TypeSafe/Jev executa zero retries automáticos: não repete a chamada
por conta própria nem empilha uma política de retry do SDK com a do RAG. Cada
resposta é classificada em um estado explícito:

| Estado | Situação |
| --- | --- |
| `ok` | Resposta estruturada, modelo e uso validados. |
| `timeout` | A chamada HTTP ou a espera pelo trabalho excedeu o limite. |
| `rate_limited` | O provider respondeu HTTP `429`. |
| `auth_error` | O provider respondeu HTTP `401` ou `403`. |
| `provider_error` | O provider respondeu `529` ou `5xx`, ou ocorreu falha de transporte. |
| `invalid_response` | JSON, chaves, IDs, tipos, números, labels ou modelo divergentes; também cobre status HTTP inesperado. |
| `budget_exhausted` | Deadline, orçamento da etapa ou reserva mínima não permitem iniciar a chamada. |
| `capacity_exhausted` | Não havia slot global disponível no processo. |

Respostas inválidas não são apresentadas como decisões parciais ou como um
booleano de aprovação. Falhas de configuração e estados operacionais devem
seguir o fallback do consumidor; nenhum deles libera uma decisão semântica
fictícia.

### Telemetria, custo e conclusões tardias

As chamadas Jev entram em `model_calls` e no `ASK_TRACE` com os mesmos
identificadores locais do pedido. O registro contém somente metadados
operacionais:
`provider`, `stage`, `request_id`, `call_id`, `candidate_id`, `attempt`,
`requested_model`, `model`, `status`, `latency_ms`, `usage`,
`estimated_cost_usd`, `cost_status`, `pricing_version`, `error_code`,
`completion_status` e `late_completion`. `candidate_id` identifica o candidato
avaliado; não altera `attempt` nem representa retry.

O trace não registra `TYPESAFE_API_KEY`, headers, `state`, perguntas, respostas
brutas, conteúdo do provider ou a representação de exceções. Os IDs são opacos
e os códigos de erro são sanitizados. A configuração do cliente não expõe o
endpoint como opção arbitrária.

O custo estimado usa `pricing_version=typesafe-2026-09-21` e a tarifa de
entrada de US$ 0,042 por milhão de tokens; a saída Jev não tem tarifa nessa
tabela. Com `input_tokens` conhecido, o custo de entrada pode ser calculado
mesmo quando `output_tokens` é nulo. Sem uso de entrada, o custo permanece
`null` com `cost_status=usage_unavailable`; falhas sem uso também não são
convertidas em custo zero.

Se a espera do chamador terminar antes do worker, o resultado devolvido fica
em `timeout` com `completion_status=pending`. A conclusão posterior é
correlacionada por `call_id`, `request_id` e `candidate_id` no metadado de
conclusões tardias e recebe `completion_status=completed_late`; ela não reabre
nem altera a resposta ou o trace já finalizado.

### Rollback

Desabilitar os consumidores Jev deixa o cliente sem uso e sem chamadas. Para
reverter a entrega, remova o módulo e as configurações da issue #89 junto com
as referências consumidoras correspondentes. O rollback não exige migração de
banco, alteração de schema, reindexação, recálculo de embeddings ou
reingestão.

## Reranking Jev (issue #91, entrega pendente nesta branch)

O perfil padrão mantém `RAG_RERANK_PROVIDER=existing` e o reranker atual.
Para usar Jev diretamente no caminho de resposta, configure
`RAG_ENABLE_RERANKING=true`, `RAG_RERANK_PROVIDER=jev` e uma
`TYPESAFE_API_KEY` válida. Com `RAG_ENABLE_RERANKING=false`, nenhum reranker
é chamado. A opção `jev` altera a ordem dos candidatos usados para montar o
contexto do gerador; ela não ativa um período de observação e não muda a
recuperação, a geração nem as regras de abstenção.

Perfil de ensaio ativo, após disponibilizar a chave no ambiente seguro da
instalação:

```dotenv
RAG_ENABLE_RERANKING=true
RAG_RERANK_PROVIDER=jev
JEV_MODEL=jev-1.13.0
```

| Variável | Default | Finalidade |
| --- | ---: | --- |
| `RAG_RERANK_PROVIDER` | `existing` | Seleciona `existing` ou `jev`; qualquer outro valor é inválido. |
| `JEV_RERANK_MAX_CANDIDATES` | `20` | Limita a avaliação aos primeiros candidatos recuperados; aceita `2` a `40`. |
| `JEV_MAX_STATE_ESTIMATED_TOKENS` | `24000` | Teto conservador para o `state` e a pergunta enviados em cada decisão; aceita `1` a `24000`. |

Jev classifica a relevância de cada candidato selecionado com uma pergunta
Noul. A pontuação apenas reordena: nenhum candidato é removido por limiar, e
os que excedem o limite de quantidade permanecem na cauda original. O
conteúdo selecionado é enviado por inteiro; se exceder o orçamento estimado,
a etapa Jev é descartada sem truncamento silencioso. O limite usa um
estimador local e não garante a contagem exata do provider.
Os candidatos reordenados recebem `jev.state_sha256`, hash do `state`
efetivamente enviado, para referência pelo avaliador. O hash não inclui a
posição de recuperação e não expõe o conteúdo no `ASK_TRACE`.

Uma ordem Jev só é aplicada se todas as decisões selecionadas forem válidas.
Em falha, o fluxo tenta o reranker `existing` apenas quando a condição
original dele e a reserva do prazo ainda permitem; caso contrário conserva
a ordem recuperada. `ASK_TRACE` registra provider solicitado e efetivo,
aplicação, contagens, motivo de fallback, versão do prompt, latência e custo
conhecido, sem copiar pergunta, conteúdo, credencial ou resposta bruta.
Cada candidato gera uma chamada distinta com `attempt=1`; não é retry.
Se a busca global de fallback for acionada depois do primeiro contexto, o
pipeline pode executar uma segunda passagem de reranking sobre os candidatos
combinados; o limite de candidatos vale para cada passagem. O custo total e
os dois registros ficam associados à mesma pergunta.

Para voltar ao comportamento anterior, configure
`RAG_RERANK_PROVIDER=existing` ou desabilite `RAG_ENABLE_RERANKING`. O
rollback não exige migração, reindexação nem recálculo de embeddings. A
comparação operacional ativa depende do runner da issue #90 e da rodada da
issue #93; os testes sintéticos desta entrega não demonstram ganho real.

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
| `RAG_MAX_INPUT_TOKENS` | 24000 | 24000 | limitar o prompt de entrada antes da reserva de saída e margem |
| `RAG_MAX_HISTORY_TOKENS` | 4000 | 4000 | reservar um teto independente para o histórico da conversa |
| `RAG_MODEL_CONTEXT_TOKENS` | 32768 | 65536 | declarar explicitamente a janela do perfil, sem inferir pelo nome do modelo |
| `RAG_CONTEXT_MARGIN_TOKENS` | 512 | 512 | manter margem configurável para o limite da janela do provider |
| `RAG_IMAGE_TOKEN_RESERVE` | 1024 | 1024 | reservar espaço conservador para imagens anexadas |
| `SIMILARITY_THRESHOLD` | 0,55 | 0,40 | recuperar mais candidatos para as etapas de filtro |
| `RAG_MIN_STRONG_SIMILARITY` | 0,62 | 0,42 | calibrar a abstenção ao corpus atual |
| `RAG_OPERATIONAL_SIMILARITY_MARGIN` | 0,05 | 0,03 | usar margem menor no perfil calibrado |
| `RAG_MAX_REGEN_ATTEMPTS` | 1 | 2 | permitir uma correção adicional de grounding |
| `ASK_TIMEOUT_SECONDS` | 120 | 240 | acomodar respostas extensas em produção |
| `ASK_MAX_CONCURRENCY` | 4 | 4 | limitar o trabalho simultâneo aceito pelo processo |
| `ASK_MAX_TOKENS` | 8192 | 16384 | permitir o limite operacional configurado |
| `MAX_HISTORY_PAIRS` | 20 | 12 | limitar o crescimento do contexto por conversa |
| `CONTEXTUAL_RETRIEVAL_ENABLED` | false | false | manter o contexto determinístico escolhido no benchmark da issue #16 |
| `CONTEXTUAL_RETRIEVAL_MAX_TOKENS` | 150 | 250 | enriquecer chunks com mais contexto no perfil atual |
| `CONTEXTUAL_RETRIEVAL_BATCH_SIZE` | 50 | 20 | reduzir a pressão por lote sobre o provider |

Para conferir a configuração sem expor credenciais, use `!status`: o resumo
mostra providers e modelos ativos, mas não mostra chaves.

### Orçamento do contexto RAG

O pipeline calcula o orçamento do prompt antes da geração, separando conteúdo
fixo/políticas/pergunta, histórico, evidências e saída. `RAG_MAX_INPUT_TOKENS`
limita a entrada; `ASK_MAX_TOKENS` (e, para OpenAI,
`OPENAI_MAX_OUTPUT_TOKENS`) reserva a saída; `RAG_CONTEXT_MARGIN_TOKENS`
reserva uma margem adicional; e `RAG_MODEL_CONTEXT_TOKENS` declara a janela
conhecida do perfil. A soma não é inferida pelo nome do modelo. Se o conteúdo
fixo já exceder o limite, a pergunta termina com estado explícito de orçamento
excedido, sem descartar silenciosamente regras ou a pergunta.

Quando `tiktoken` está disponível para o provider OpenAI, ele é usado sem
chamada paga. Nos demais casos, o fallback conservador versionado
`utf8-bytes-div2-ceil-v1` estima tokens por bytes UTF-8 e é aplicado também a
português, Unicode, SQL e tabelas. O trace registra a versão, método, contagem,
orçamentos, evidências mantidas e motivos das exclusões, sem registrar o texto
bruto. A seleção preserva a ordem do reranker, remove duplicatas e sobreposição
antes de contar o orçamento e deriva `allowed_sources` somente das evidências
retidas.

## Contextualização da ingestão

O padrão é `CONTEXTUAL_RETRIEVAL_ENABLED=false`. Assim, a ingestão usa somente o
contexto determinístico de título e hierarquia de headings acrescentado durante
o chunking, sem chamada de modelo para contextualizar os trechos. Essa decisão
segue o benchmark da issue #16, no qual a variante por LLM piorou Recall@10,
Recall@20 e nDCG@10, aumentou falsas alegações de ausência e tornou a ingestão
213,4% mais lenta.

Definir `CONTEXTUAL_RETRIEVAL_ENABLED=true` habilita explicitamente o modo
experimental por LLM. Nesse modo, a ingestão gera uma frase curta para situar
cada trecho antes de criar seu embedding. O conteúdo original continua armazenado
em `document_chunks.content` e é o único apresentado como evidência; o texto
gerado pelo modelo não vira citação do documento.

O texto exato enviado ao embedding é persistido separadamente em
`document_chunks.retrieval_text` e também alimenta o índice full-text usado pela
busca híbrida. `document_chunks.contextualization_version` identifica o contrato
que realmente acrescentou contexto ao trecho; permanece nulo quando a
contextualização está desativada ou quando ocorre fallback para o texto sem
contexto gerado.

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

As mudanças de contrato não iniciam ingestão, embeddings ou chamadas externas
automaticamente. A migração `sql/add_section_retrieval_1536.sql` preenche
`retrieval_text` com o conteúdo original existente, sem inferência. Na próxima
execução explícita de `ingest.py` ou `!ingerir`, documentos sob uma identidade de
processamento diferente serão detectados como alterados e o texto de retrieval
exato será gravado. Desativar a contextualização não executa essa reingestão
automaticamente. Planeje a janela operacional antes de executá-la; a versão
válida do índice é preservada se a preparação falhar.

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
