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
| `evidence_discounted_coverage_at_10` | Cobertura descontada das referências até o corte 10; não é nDCG e permite várias referências no mesmo chunk. |
| `ndcg_at_10` | nDCG convencional em um universo julgado explícito por `candidate_id`; é `null` com qrels ausentes, candidato não julgado ou IDCG zero. |
| `citation_validity` | Citação de fonte de referência sem erro de grounding. |
| `behavior_match` | Correspondência entre responder, esclarecer ou abster-se e o esperado. |
| `false_abstention` / `false_absence_claim` | Abstenção indevida e alegação de ausência em pergunta respondível. |
| `p50_latency_ms` / `p95_latency_ms` | Mediana e cauda de latência dos casos. |
| `model_usage` | Todas as chamadas observadas, tokens, custo conhecido e completude do custo. |

`outcomes` separa respostas corretas, respostas sem suporte, abstenções corretas e
indevidas, esclarecimentos necessários e desnecessários. Cada taxa traz contagem,
denominador e intervalo de confiança de Wilson de 95%. `answerable_coverage` informa
quantas perguntas respondíveis receberam uma resposta, separadamente da correção.
O relatório repete métricas e outcomes por `development` e `holdout` e avalia, apenas
no holdout, os critérios predefinidos em `baseline_config.json`.

Scores de similaridade, fusão ou reranking são sinais de ordenação e não devem ser
interpretados como probabilidade de a resposta estar correta.

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
- Recall@20 fica limitado se o pipeline devolver menos de 20 candidatos; cada caso
  registra `retrieved_depth` no detalhe da métrica.
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
