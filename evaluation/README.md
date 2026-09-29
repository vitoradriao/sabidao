# Avaliação do RAG do maxPedido

[Documentação](../docs/README.md) / Avaliação

Este diretório mantém um baseline reproduzível para medir recuperação, decisão de
responder, esclarecimento, abstenção, correção factual, latência e custo. O baseline
inicial tem 30 casos: 20 de desenvolvimento e 10 de holdout.

## Estado da validação

Os testes automatizados exercitam o cálculo de métricas e os contratos do
avaliador. O runner, o dataset e a política descritos aqui formam a
infraestrutura de avaliação entregue na issue #18; eles não comprovam, por si
sós, a qualidade do sistema em operação. Os 30 casos tiveram as perguntas
revisadas pelo mantenedor em 21/09/2026, sem divergências registradas, mas ainda
não há baseline operacional aprovado. Esse trabalho continua na
[issue #53](https://github.com/vitoradriao/sabidao/issues/53), que exige ambiente,
snapshot, orçamento e resultados sanitizados identificados antes de qualquer
declaração de validação operacional.

| Arquivo | Finalidade |
| --- | --- |
| `baseline_config.json` | Política congelada e critérios de não regressão definidos antes do holdout. |
| `datasets/maxpedido_seed_cases.json` | Fonte curada dos 30 casos. |
| `datasets/maxpedido_eval_dataset.json` | Dataset normalizado usado pelo runner. |
| `datasets/evaluator_synthetic_fixture.json` | Fixture sem banco ou API para testar o avaliador. |
| `datasets/migration_batch_synthetic_fixture.json` | Casos sintéticos para exercitar consultas de um lote canônico. |
| `migration_batch_policy.template.json` | Modelo não aprovado para congelar o ensaio de um lote. |
| `verify_migration_batch.py` | Compara relatórios de referência e candidato sem chamar serviços. |
| `build_dataset.py` | Normaliza os casos e acrescenta, opcionalmente, lacunas do banco. |
| `run_offline_eval.py` | Executa o RAG, calcula métricas e produz o relatório. |
| `run_contextual_ingest_benchmark.py` | Mede tempo, chunks e uso de providers na ingestão das variantes da issue #16. |

Para verificar uma migração documental por lote, consulte o
[procedimento de preservação e consultas](../docs/verificacao-migracao-canonica.md).

## Proveniência e revisão

Cada caso identifica `provenance`, `review`, `split` e `answerability`. Os casos
respondíveis foram conferidos contra uma fonte versionada no repositório; os casos
sintéticos declaram explicitamente a entidade inventada ou a ambiguidade usada. A
revisão inicial foi automatizada. Em 21/09/2026, o mantenedor `vitoradriao`
confirmou as perguntas dos 30 casos; a revisão está registrada como
`human_review: approved`, com revisor, data e divergências.

Lacunas importadas de `knowledge_gaps` também recebem `review.status` igual a
`pending_human`. Antes de promovê-las ao holdout ou tratá-las como verdade de
referência, uma pessoa deve confirmar que a lacuna continua válida e registrar a
revisão no caso.

Os valores de `answerability` são:

- `answerable`: deve haver resposta sustentada pelos fatos e evidências anotados;
- `ambiguous`: faltam dados e o comportamento esperado é pedir esclarecimento;
- `no_evidence`: a resposta correta é a abstenção.

`expected_facts` lista fatos obrigatórios, `forbidden_facts` lista afirmações que
constituem falha crítica e `reference_evidence` identifica fonte e termos esperados no
retrieval. Referências legadas continuam aceitando o caminho exato em `source`; o
avaliador não infere identidade pelo basename. Para uma fonte canônica, declare
`canonical_id` e, quando relevantes, `document_revision`, `section_key`, `source_id`
e `locator`. `source` pode ser um alias editorial somente junto da identidade
canônica explícita; splits precisam apontar para a seção ou evidência correta.
Por exemplo:

```json
{"source": "docs/antigo.md", "canonical_id": "10000000-0000-4000-8000-000000000001", "document_revision": 2, "section_key": "conta-corrente", "source_id": "manual", "locator": {"kind": "line_range", "start": 10, "end": 12}, "contains": ["Passos confirmados"]}
```

Para avaliar a ordenação com nDCG convencional, um caso também pode declarar
`ranking_judgments` com `schema_version: 1`, `universe_id`, `corpus_fingerprint` e
`qrels` (`candidate_id` + relevância inteira de 0 a 3). Os `candidate_id` dos chunks
retornados precisam ser únicos e estar julgados; o nDCG usa somente os dez primeiros.
O hash canônico
dos qrels é registrado no detalhe, sem copiar o conteúdo bruto para o relatório.
O fingerprint do experimento inclui o manifesto canônico e o hash sanitizado da
projeção persistida, incluindo revisão, seção, proveniência e `retrieval_text`.
Mudanças nessa identidade exigem nova comparação; um relatório de corpus legado
não é diretamente comparável a um corpus promovido.

## Preparar o dataset

Execute na raiz do repositório, com o ambiente Python ativo:

```sh
python evaluation/build_dataset.py --knowledge-gap-limit 0
```

Para acrescentar até 20 lacunas operacionais ainda pendentes de revisão:

```sh
python evaluation/build_dataset.py --knowledge-gap-limit 20
```

A saída padrão é `evaluation/datasets/maxpedido_eval_dataset.json`.

## Calibrar e executar o baseline

Primeiro execute somente o conjunto de desenvolvimento. Esta etapa pode orientar
ajustes na política, desde que `baseline_config.json` seja atualizado e versionado
antes de abrir o holdout:

```sh
python evaluation/run_offline_eval.py --dry-run --split development
```

Depois de congelar configuração, modelos, índice, critérios e limiares, execute o
holdout sem reajustar a solução a partir dos resultados:

```sh
python evaluation/run_offline_eval.py --dry-run --split holdout
```

Para produzir o relatório consolidado:

```sh
python evaluation/run_offline_eval.py --dry-run --split all
```

Apesar do nome “offline”, o runner usa os serviços de banco e IA configurados. A
opção `--dry-run` impede escrita nas tabelas de avaliação, mas não elimina chamadas
externas nem custo. Nenhuma chamada paga é executada pelos testes unitários.

Para persistir uma execução, prepare as tabelas e retire `--dry-run`:

```sh
psql "$DATABASE_URL" -f sql/migrate_evaluation_metrics_v2.sql
psql "$DATABASE_URL" -f sql/add_evaluation_identity.sql
python evaluation/run_offline_eval.py --split all
```

`add_evaluation_identity.sql` calcula os hashes no PostgreSQL e retorna somente
contagens, fingerprints e identidades vetoriais; o relatório não recebe o conteúdo
bruto do corpus ou das correções. Sem a migração ou sem banco configurado, a
identidade de dados aparece como `unknown`, nunca como um snapshot presumido.

É possível limitar casos e definir o relatório:

```sh
python evaluation/run_offline_eval.py --limit 10 --output-report evaluation/reports/latest.json
```

Relatórios gerados em `evaluation/reports/` são ignorados pelo Git.

### Comparar execuções

Para comparar com um relatório anterior, informe a referência. Diferenças não
declaradas e identidades desconhecidas deixam `runtime_comparison.compatible` como
`false`, salvam o relatório para diagnóstico e fazem o comando terminar com código 1:

```sh
python evaluation/run_offline_eval.py --dry-run --split all \
  --compare-report evaluation/reports/reference.json
```

Em uma ablação, declare cada variável deliberadamente alterada pelo caminho exibido
em `runtime.experiment_identity`. Por exemplo:

```sh
python evaluation/run_offline_eval.py --dry-run --split all \
  --compare-report evaluation/reports/reference.json \
  --experimental-variable rag_config.RAG_GLOBAL_CHALLENGER_COUNT
```

A declaração permite somente essa diferença; mudanças adicionais continuam
incompatíveis. Ela não transforma corpus, feedback ou identidade vetorial
desconhecidos em uma comparação válida.

### Aprovação automática do holdout

Use `--gate` para consumir o resultado em automações:

```sh
python evaluation/run_offline_eval.py --split holdout --gate --output-report evaluation/reports/holdout.json
```

Esse comando executa o RAG e pode chamar providers pagos. `--dry-run` apenas
desativa a persistência no banco; não torna a execução local ou gratuita.

Todos os critérios de `non_regression` são obrigatórios. O status é `passed`
somente quando todos passam, todos os casos do holdout do dataset carregado são
executados e há casos `answerable`, `ambiguous` e `no_evidence`. Uma taxa sem
denominador permanece indisponível; não conta como sucesso.

Uma falha em qualquer critério, crítico ou não, produz `failed`. Falhas têm
precedência sobre lacunas de avaliação, que continuam visíveis em `complete`,
`missing_checks` e `coverage`. `critical_failures` identifica as falhas críticas.
Sem falhas conhecidas, critérios indisponíveis, classes ausentes ou holdout parcial
produzem `incomplete`. Uma política ausente não aprova o gate.

Com `--gate`, a saída é `0` apenas para `passed` e `1` para os demais resultados;
o relatório é salvo antes dessa saída. Sem a opção, execuções concluídas mantêm
saída `0` para exploração, mesmo com `failed` ou `incomplete` no relatório.
`--split development` não avalia o holdout. `--limit` só permite aprovação se
a seleção ainda incluir todo o holdout; limitar desenvolvimento não impede a
aprovação quando todos os casos do holdout foram executados.

A cobertura é relativa ao dataset fornecido. Isso não certifica sua revisão humana,
identidade experimental ou validade operacional; essas validações continuam
necessárias antes de tratar o resultado como um baseline de produção.

## Comparação pareada com Jev

A issue #90 entrega o contrato e o runner para a comparação ativa. O
baseline registra as variantes com identificadores estáveis:

- `existing` (A): pipeline atual;
- `jev_rerank` (B): reranking Jev ativo implementado na issue #91;
- `jev_rerank+evidence_gate` (C): B com o gate de suficiência da issue #92;
  exige política explícita. O perfil padrão mantém C indisponível por falta
  dessa política, sem bloquear o primeiro A/B.

O perfil também congela `pair_id`, `snapshot_id`, as políticas de identidade, a
janela de evidência e o construtor de contexto. Uma variante indisponível falha
explicitamente quando solicitada; ela nunca é substituída silenciosamente pela
variante A. O relatório registra `effective_variant`, fallback e motivo, e um
fallback para `existing` não conta como sucesso de Jev.

Antes de executar providers, valide a preparação local. `--prepare-only` acessa
somente o dataset, o perfil, a política local quando C for solicitada e o relatório local; não chama banco,
modelo, embeddings, rede ou escreve tabelas:

```sh
python evaluation/run_offline_eval.py --prepare-only \
  --output-report evaluation/reports/jev-prepare.json
```

O status `blocked` é esperado enquanto o snapshot não estiver registrado.
`--dry-run` tem outro significado: impede apenas a escrita das tabelas e ainda
pode chamar banco e providers pagos.

Com um perfil de teste isolado e a credencial TypeSafe configurada, execute o par
informando explicitamente o snapshot:

```sh
python evaluation/run_offline_eval.py --paired \
  --snapshot-id <snapshot-congelado> \
  --pair-id <par-estavel> \
  --dry-run --output-report evaluation/reports/jev-paired.json
```

O runner transmite o `snapshot_id` informado e a `experiment_identity` observada
ao provider pareado, depois rejeita retorno com identidade ou snapshot divergente.
As fixtures sintéticas exercitam o runner com providers fake, sem chamadas pagas.
A execução real exige dados permitidos, perfil e orçamento definidos para a #93.
`snapshot_id` identifica o par, mas não cria uma transação de banco congelada: o
runner confere a identidade observada antes e depois de cada variante. O
responsável pelo ensaio deve manter corpus e feedback estáveis durante a rodada.

O harness produz duas visões do mesmo recorte: `ranking_ablation_same_pool`,
que reutiliza o mesmo conjunto de candidatos e a mesma consulta reformulada da primeira variante
para controlar a entrada (A em A/B; B em B/C), e
`end_to_end_same_snapshot`, que executa cada caminho independentemente no mesmo
snapshot. Cada resposta registra os estágios `sections`, `candidate_pool`,
`post_rerank`, `post_gate` e `final_context`, com ordem, contagem, exclusões,
IDs opacos e métricas com denominador e motivo de indisponibilidade. O envelope
final preserva hashes, fontes e spans, sem texto bruto nos traces persistidos ou
no `ASK_TRACE`.
Na ablação, o relatório guarda somente `same_rerank_query`; o texto e seu hash
não são publicados.

O reranker existente recebe uma janela menor do texto do candidato que o Jev.
O perfil registra essa diferença e o relatório deixa `gain_attribution` como
`unavailable`; um eventual ganho de B não pode ser atribuído apenas ao modelo
sem um controle de janela equivalente.

O modo efetivo do reranker Jev (`JEV_RERANK_MODE=pointwise|batch`) participa da
identidade experimental. A matriz JEV-11 compara os braços explícitos
`jev_rerank_pointwise` e `jev_rerank_batch` com o mesmo pool, consulta
reformulada, texto integral e cap de estado. Versões de prompt, agrupamento,
estimador e composição ficam congeladas no perfil; como os prompts históricos
são diferentes, o relatório mantém esse confundidor declarado e não atribui um
eventual ganho somente ao batching.

O resultado desta issue não aprova adoção, custo ou operação. Revisão humana,
ensaio operacional, orçamento e decisão de adoção continuam nas issues #53,
#93 e #96. A variante B deve registrar a aplicação efetiva do Jev e o hash opaco
do `state` enviado; fallback para `existing` não conta como sucesso de Jev.

### Matriz JEV-11: batching, gate e grounding D0/D1

Esta seção descreve a entrega da #115 pendente de integração. Os schemas 11/3
e os novos comandos passam a valer com o PR desta entrega; a validação sintética
não conclui os ensaios operacionais #93/#96.

O bloco `comparison.jev_study` congela quatro experimentos relacionados, mas
separados:

- `existing` versus `existing+evidence_gate`, para medir o gate isoladamente;
- `jev_rerank_pointwise` versus `jev_rerank_batch`, para a ablação de ranking;
- `grounding_d0` versus `grounding_d1`, usando o mesmo pipeline-base escolhido
  em desenvolvimento e permitindo diferenças apenas em extração, julgamento e
  regeneração;
- julgamento de uma resposta-base fixa contra o mesmo envelope exato, para
  isolar erro do juiz sem chamar esse ensaio de shadow.

Primeiro prepare a matriz sem acesso externo:

```sh
py -3.11 -B evaluation/run_offline_eval.py --jev-study --prepare-only \
  --split development \
  --baseline-config evaluation/baseline_config.json \
  --dataset evaluation/datasets/jev_study_synthetic_fixture.json \
  --output-report evaluation/reports/jev-study-prepare.json
```

`--prepare-only` registra zero chamadas e zero escritas. A ausência de
`snapshot_id`, `TYPESAFE_API_KEY` ou política local aparece como bloqueio; não é
sucesso parcial. `--dry-run` continua podendo consultar banco e modelos.

Antes de executar, copie o perfil para um arquivo local e registre o snapshot,
as políticas e `jev_study.grounding.base_selection_development_run_id`: o run de
development que escolheu `base_variant`. O valor `existing` no modelo de perfil
é ilustrativo. Para política provisional, registre também
`jev_study.grounding.development_run_id` e
`comparison.evidence_gate.development_run_id`, iguais ao ID da política. Nenhum
limiar calibrado ou orçamento de execução é fornecido nesta entrega.

Com dados e orçamento autorizados, execute a matriz:

```sh
python evaluation/run_offline_eval.py --jev-study --split development --dry-run \
  --baseline-config runtime/perfil-jev-study.json \
  --dataset runtime/development-permitido.json --snapshot-id SNAPSHOT_CONGELADO \
  --fixed-responses runtime/respostas-base.json \
  --output-report evaluation/reports/jev-study.json
```

`--fixed-responses` contém uma lista privada, com um registro por `case_id`:
`answer`, `answer_sha256`, `envelope`, `envelope_sha256`. O envelope contém
`version=context-selection-v3`, `rendered_text` integral, `retained_chunks` e
`evidence` exatamente da seleção usada pela geração. O hash da resposta usa
SHA-256 de seus bytes UTF-8; o hash do envelope usa JSON com `ensure_ascii=False`,
`sort_keys=True`, `separators=(",", ":")`. O validador verifica hashes, spans e
conteúdo renderizado antes das calls. Esse ensaio executa somente extração e
julgamento, sem nova geração, recuperação ou regeneração. Preserve o arquivo
no local permitido; não publique textos, credenciais ou revisão detalhada.
O mesmo argumento junto de `--prepare-only` valida o arquivo sem calls.

O relatório só fica `complete` quando as três comparações, o julgamento fixo e
a auditoria de ordem estão completos. Sem respostas fixas, a comparação do
pipeline é executada, mas a matriz fica `incomplete`. Providers separados são
aceitos pela API para fixtures. `complete` significa execução do protocolo,
não aprovação da qualidade ou da revisão humana.

A auditoria executa novamente apenas o reranker, com a lista original, reversa
e uma rotação estável dos mesmos candidatos sob o mesmo cap. Ela registra
hashes de ordem, composição de chamadas e scores por ID opaco. Empates também
podem mudar a ordem; os scores permitem distinguir esse efeito. O custo da
auditoria fica separado do custo de cada pipeline e participa de
`total_model_usage` junto das comparações e do julgamento fixo. A auditoria
é restrita a `development`; não altera ensemble, política ou holdout.
As fixtures `tests/test_jev_evaluation_study.py` exercitam A/B/C, gate isolado,
pointwise/batch, D0/D1 e julgamento fixo pelo pipeline real com providers
simulados, sem rede paga.

D0 e D1 congelam pipeline-base, gate, modelos, prompts comuns, corpus, feedback,
strict e budgets. O trace seguro conserva decisões, probabilidades, conflito,
inconclusivo, extração, rodadas inicial/final, regenerações e qualificação da
resposta quando observados. IDs de claim, pergunta e chamada são opacos; textos,
respostas, referências e revisão detalhada permanecem locais. Uma falha de
infraestrutura ou deadline fica inconclusiva e nunca é contabilizada como
melhora factual.

Uma confirmação exige outro dataset independente, comparações pré-registradas e
política congelada. Use `--paired` e um perfil por comparação com
`jev_study.dataset_role=confirmation`. Registre `independent_dataset_id` igual
a `--dataset-name`, `dataset_sha256` calculado pelo mesmo JSON canônico,
`policy_status=frozen`, `policy_frozen=true` e `preregistered_comparisons` (por
exemplo, `grounding_d0_vs_grounding_d1`). O runner verifica a política real,
a revisão humana dos casos e o hash; rejeita comparação não registrada,
IDs históricos e perguntas/casos do baseline histórico mesmo renomeados.
Não use `--jev-study` para confirmação: a matriz escolhe a base e audita ordem
somente em development. D0/D1 aceita apenas a flag de grounding como diferença
na identidade; corpus, feedback, modelos, prompts comuns e budgets divergentes
são rejeitados antes do segundo braço.

A revisão semântica usa `grounding_review`, separada da revisão das perguntas.
Informe `status=completed`, `reviewer`, `date`, `answer_sha256` da resposta
revisada e `claims` com `claim_id`, `gold_support`, `extraction_found` e, quando
encontrada, `extracted_claim_id`. O artefato público preserva contagens,
cobertura e matriz de confusão; o detalhe fica no arquivo privado. Uma revisão
de outra resposta permanece `not_performed`. Resposta qualificada exige uma
rodada rejeitada seguida de regeneração e suporte final; suporte direto não
é qualificação, e conflito original não é erro residual da atribuição validada.

### Comparação B/C com política explícita

Em uma cópia do perfil, configure `comparison.variants` como
`["jev_rerank", "jev_rerank+evidence_gate"]` e remova C de
`comparison.unavailable_variants`. Preserve os controles, corpus, modelos,
budgets e strict. A variante B desliga o gate e C o ativa, independentemente
da flag no ambiente. O formato da política obrigatória em `JEV_POLICY_FILE`
está no [guia de configuração](../docs/configuracao.md#gate-de-suficiência-jev-issue-92).

Para política `provisional`, use `--split development` e registre
`comparison.evidence_gate.development_run_id` igual ao valor da política.
`--split holdout` e `--split all` exigem `frozen`. A validação ocorre antes de
chamar qualquer variante, inclusive B. O hash integral da política participa
da identidade de ambos os braços; a flag ativa é o fator experimental. Cada
resultado C deve registrar a mesma política da identidade da execução.

```sh
python evaluation/run_offline_eval.py --prepare-only --split development \
  --baseline-config evaluation/perfil-jev-bc.json \
  --output-report evaluation/reports/jev-bc-prepare.json
```

O perfil do exemplo deve ser preparado pelo responsável; ele não é fornecido
com limiares inventados. Após autorização dos dados e orçamento na #93, use o
mesmo perfil com `--paired`, `--snapshot-id` e `--pair-id` para executar. A
preparação não executa a comparação paga.

O schema preparado na #115 é 11 e o da comparação é 3. `evidence_gate` agrega
contagens por status, decisão aplicada e motivo. O trace por caso conserva a
política, a decisão e seu custo na etapa `evidence_gate`. Gate inconclusivo ou
indisponível é fallback, não sucesso C; se o reranker falhar mas o gate atuar,
a variante efetiva é `existing+evidence_gate`. Uma negativa aceita conserva a
evidência de `post_gate`, mas não a contabiliza como enviada ao gerador.
`unsupported_claims` continua não avaliado na abstenção ou esclarecimento.

Fixtures da integração completa B/C, com transportes simulados:

```sh
python -m unittest tests.test_jev_evidence_gate tests.test_offline_eval
```

Esses testes verificam execução e contratos. Não demonstram qualidade semântica,
calibração em português ou resistência a injeção; essas medidas pertencem à #93.

## Conteúdo do relatório

O bloco `runtime` registra commit, hash do dataset e `experiment_identity`. Essa
identidade inclui configuração efetiva de roteamento, challenger, seções,
diversidade, reformulação, contexto, grounding e geração; hashes dos prompts e das
políticas; controles de cache; snapshot sanitizado do corpus e do feedback elegível;
e identidades vetoriais configuradas e persistidas por escopo. O fingerprint é
canônico e não depende da ordem de retorno dos registros. Estados `configured`,
`verified`, `mismatch` e `unknown` permanecem distintos.

| Campo | O que acompanha |
| --- | --- |
| `factual_correctness` | Presença de todos os fatos obrigatórios predefinidos. |
| `unsupported_claims` | Proxy de fatos proibidos e erros do validador de grounding. |
| `recall_at_10` / `recall_at_20` | Fração das referências distintas encontrada até cada corte. |
| `recall_at_40` / `evidence_coverage_at_20` / `evidence_coverage_at_40` | Diagnósticos de cobertura com denominador e motivo explícito de indisponibilidade. Não são nDCG. |
| `evidence_discounted_coverage_at_10` | Cobertura descontada das referências até o corte 10; não é nDCG e permite várias referências no mesmo chunk. |
| `ndcg_at_10` | nDCG convencional em um universo julgado explícito por `candidate_id`; é `null` com qrels ausentes, candidato não julgado ou IDCG zero. |
| `citation_validity` | Citação de fonte de referência sem erro de grounding. |
| `behavior_match` | Correspondência entre responder, esclarecer ou abster-se e o esperado. |
| `false_abstention` / `false_absence_claim` | Abstenção indevida e alegação de ausência em pergunta respondível. |
| `p50_latency_ms` / `p95_latency_ms` | Mediana e cauda de latência dos casos. |
| `model_usage` | Chamadas físicas deduplicadas por `call_id`, tokens, custo conhecido/desconhecido, conclusão tardia e falhas de deadline. |

`outcomes` separa respostas corretas, respostas sem suporte, abstenções corretas e
indevidas, esclarecimentos necessários e desnecessários. Cada taxa traz contagem,
denominador e intervalo de confiança de Wilson de 95%. `answerable_coverage` informa
quantas perguntas respondíveis receberam uma resposta, separadamente da correção.
O relatório repete métricas e outcomes por `development` e `holdout` e avalia, apenas
no holdout, os critérios predefinidos em `baseline_config.json`.

Scores de similaridade, fusão ou reranking são sinais de ordenação e não devem ser
interpretados como probabilidade de a resposta estar correta.

Os diagnósticos `evidence_coverage_at_20/40` exigem `review.human_review=approved`
para as referências do caso. Sem confirmação da revisão, o valor fica `null`
com motivo `reference_review_not_confirmed`; a contagem de referências continua
visível. Isso não impede executar fixtures nem inventa qrels para nDCG.

### Definições e compatibilidade de métricas

O relatório registra `evaluator_schema_version`, `metric_definitions_version` e o hash
das definições. Relatórios com versões diferentes não são comparados silenciosamente:
é necessário reavaliar ou declarar explicitamente a diferença na comparação. Recall
continua contando cada referência no máximo uma vez. A cobertura descontada usa:

```text
sum(1 / log2(primeiro_rank + 1) para cada referência encontrada até 10)
-----------------------------------------------------------------------
                         total de referências
```

O nDCG usa `gain = 2^relevance - 1`, o mesmo universo julgado para DCG e IDCG e, no
máximo, os dez primeiros resultados. Um pool julgado não representa qualidade global
do corpus: a métrica é nDCG dentro do universo identificado por `universe_id`.
Quando a anotação não permite um cálculo válido, o valor permanece `null` e o detalhe
expõe `unavailable_reason`; ausência de anotação nunca é convertida em irrelevância ou
em zero.

Chamadas de embedding e acertos de cache aparecem em `external_calls`. Quando o
provider não expõe uso ou preço suficiente, o custo conhecido continua visível, mas
`cost_complete` fica `false`; o relatório não transforma custo ausente em zero.

## Limitações

- O conjunto inicial é pequeno; os intervalos de confiança tornam a incerteza visível.
- Correção factual usa frases aceitas e não reconhece toda paráfrase possível.
- `unsupported_claims` não substitui revisão semântica humana.
- Cobertura/recall nos cortes 20 e 40 ficam limitados se o pipeline devolver menos
  candidatos; cada caso registra `retrieved_depth` no detalhe da métrica.
- O baseline não usa LLM-as-judge. Se esse método for adotado, versão do juiz, prompt,
  ordem, viés de provider e concordância com revisão humana precisam ser medidos.
- A revisão humana confirma as perguntas do conjunto inicial, mas não transforma o
  dataset pequeno em verdade regulatória ou contratual.

## Teste determinístico do avaliador

O comando abaixo usa apenas fixtures locais e não chama banco nem provider:

```sh
python -m unittest tests.test_offline_eval
```

Compare soluções apenas com dataset, política e holdout congelados. Mudanças no
provider, modelo, índice ou configuração devem aparecer no relatório e impedir uma
comparação silenciosa entre execuções incompatíveis.

## Benchmark de contextualização da ingestão

O runner da issue #16 deve ser executado uma vez em cada banco isolado. Ele força
somente a variante informada, registra um fingerprint sanitizado do corpus e exige
uma confirmação explícita de que `DATABASE_URL` não aponta para produção. O segundo
comando compara commit, corpus e configuração invariável com o primeiro relatório e
recusa o mesmo alvo de host, porta e banco. A variante `llm` exige o relatório
`deterministic` como referência:

```sh
python evaluation/run_contextual_ingest_benchmark.py CORPUS_AUTORIZADO \
  --variant deterministic --database-label issue16-a \
  --confirm-isolated-database \
  --output-report evaluation/reports/issue16-ingest-a.json

python evaluation/run_contextual_ingest_benchmark.py CORPUS_AUTORIZADO \
  --variant llm --database-label issue16-b \
  --confirm-isolated-database \
  --reference-report evaluation/reports/issue16-ingest-a.json \
  --output-report evaluation/reports/issue16-ingest-b.json
```

Esses comandos chamam embeddings e, na variante `llm`, o modelo de
contextualização. A confirmação do banco não substitui a autorização prévia de
orçamento, corpus, providers, credenciais e casos. O relatório não publica nomes,
caminhos, URLs nem conteúdo do corpus. Quando o cliente de embeddings não expõe
tokens ou preço, os campos correspondentes permanecem desconhecidos e
`cost_complete` fica falso. O comando ainda salva o relatório quando a ingestão fica
incompleta, mas termina com código 1 e marca `ingestion.complete` como falso.

### Identidade da política documental (#17)

A entrega #17 introduziu `context-selection-v2` e registra a versão/hash de
`documentary_evidence_policy` na identidade experimental. Documentos e regras
retidas são dados separados da política de sistema. A variante C avalia o mesmo
texto documental enviado à geração, sem incluir regras descartadas pelo envelope.
`FULL_CONTEXT_ENABLED=true` é configuração inválida para todos os modos.
Não compare resultados anteriores como identidade equivalente; delimitação e
fixtures adversariais não medem resistência real a injeção nem suporte factual.

### Identidade de claims e grounding semântico (#94/#95)

Esta seção acompanha o código integrado da #95. O schema 11
registra `JEV_GROUNDING_MAX_CLAIMS`, `JEV_GROUNDING_MAX_REGENERATIONS`, a flag
efetiva, versões/hashes dos prompts, política integral e modelo na identidade
experimental. Chamadas de extração aparecem como `claim_extraction`; cada lote
dual-Noul aparece como `semantic_grounding`, com uso e custo próprios.

O trace seguro registra extração, estados por claim, probabilidades de suporte e
contradição, IDs de pergunta/chamada, hashes e fingerprint final. Não registra
texto bruto. Cobertura de spans e fixtures não equivalem a suporte semântico nem
aprovam qualidade operacional. A matriz D0/D1 da #115 mede o contrato; revisão
humana, confirmação independente e decisão operacional continuam na #96.

O envelope passa a `context-selection-v3`: seu `content_hash` identifica o
chunk original e os `spans` identificam a parte enviada; o perfil pareado
registra essa versão.
Relatórios v2 e v3 têm identidades diferentes.
