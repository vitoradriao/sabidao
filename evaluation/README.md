# Avaliação do RAG do maxPedido

[Documentação](../docs/README.md) / Avaliação

Este diretório mantém um baseline reproduzível para medir recuperação, decisão de
responder, esclarecimento, abstenção, correção factual, latência e custo. O baseline
inicial tem 30 casos: 20 de desenvolvimento e 10 de holdout.

| Arquivo | Finalidade |
| --- | --- |
| `baseline_config.json` | Política congelada e critérios de não regressão definidos antes do holdout. |
| `datasets/maxpedido_seed_cases.json` | Fonte curada dos 30 casos. |
| `datasets/maxpedido_eval_dataset.json` | Dataset normalizado usado pelo runner. |
| `datasets/evaluator_synthetic_fixture.json` | Fixture sem banco ou API para testar o avaliador. |
| `build_dataset.py` | Normaliza os casos e acrescenta, opcionalmente, lacunas do banco. |
| `run_offline_eval.py` | Executa o RAG, calcula métricas e produz o relatório. |

## Proveniência e revisão

Cada caso identifica `provenance`, `review`, `split` e `answerability`. Os casos
respondíveis foram conferidos contra uma fonte versionada no repositório; os casos
sintéticos declaram explicitamente a entidade inventada ou a ambiguidade usada. A
revisão inicial é automatizada e `human_review` permanece `pending`: ela não deve ser
apresentada como validação humana do domínio.

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
retrieval.

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
python evaluation/run_offline_eval.py --split all
```

É possível limitar casos e definir o relatório:

```sh
python evaluation/run_offline_eval.py --limit 10 --output-report evaluation/reports/latest.json
```

Relatórios gerados em `evaluation/reports/` são ignorados pelo Git.

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

O bloco `runtime` registra commit, hash do dataset, provider/modelos, identidade do
índice vetorial, configuração relevante do RAG e a política do baseline. Isso permite
comparar somente execuções compatíveis.

| Campo | O que acompanha |
| --- | --- |
| `factual_correctness` | Presença de todos os fatos obrigatórios predefinidos. |
| `unsupported_claims` | Proxy de fatos proibidos e erros do validador de grounding. |
| `recall_at_10` / `recall_at_20` | Fração das evidências esperadas encontrada até cada corte. |
| `ndcg_at_10` | Qualidade da posição das evidências distintas nos dez primeiros resultados. |
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
- Os casos ainda aguardam revisão humana de domínio, registrada explicitamente no
  dataset; não os use como verdade regulatória ou contratual.

## Teste determinístico do avaliador

O comando abaixo usa apenas fixtures locais e não chama banco nem provider:

```sh
python -m unittest tests.test_offline_eval
```

Compare soluções apenas com dataset, política e holdout congelados. Mudanças no
provider, modelo, índice ou configuração devem aparecer no relatório e impedir uma
comparação silenciosa entre execuções incompatíveis.
