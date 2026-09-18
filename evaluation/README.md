# Avaliação das respostas do maxPedido

[Documentação](../docs/README.md) / Avaliação

Este diretório reúne cenários de referência e ferramentas para comparar a qualidade das respostas do Sabidão.

| Arquivo | Finalidade |
| --- | --- |
| `datasets/maxpedido_seed_cases.json` | Cenários iniciais revisados. |
| `datasets/evaluator_synthetic_fixture.json` | Casos sintéticos para testar o avaliador sem banco ou API de IA. |
| `build_dataset.py` | Combina os casos usados na avaliação. |
| `run_offline_eval.py` | Executa o fluxo de consulta e geração e calcula as métricas. |

Execute os comandos na raiz do repositório, com o ambiente Python ativo.

## 1. Preparar os cenários

Para incluir até 20 lacunas de conhecimento registradas no banco:

```sh
python evaluation/build_dataset.py --knowledge-gap-limit 20
```

Para montar a base sem acrescentar essas lacunas, use `--knowledge-gap-limit 0`.

A saída padrão é `evaluation/datasets/maxpedido_eval_dataset.json`.

## 2. Executar a avaliação

Para executar sem persistir os resultados da avaliação no banco:

```sh
python evaluation/run_offline_eval.py --dry-run
```

Apesar do nome “offline”, o fluxo consulta os serviços de banco e IA configurados. A opção `--dry-run` não elimina essas chamadas e ainda gera um relatório local.

Para registrar a execução nas tabelas de avaliação:

```sh
psql "$DATABASE_URL" -f sql/migrate_evaluation_metrics_v2.sql
python evaluation/run_offline_eval.py
```

A migração é necessária uma vez em instalações que já possuíam as tabelas de avaliação.
Ela preserva os resultados anteriores e passa a representar métricas não avaliadas com
`NULL`.

Para limitar os casos e escolher o relatório:

```sh
python evaluation/run_offline_eval.py --limit 50 --output-report evaluation/reports/latest.json
```

## Métricas

| Campo | O que acompanha |
| --- | --- |
| `behavior_match` | Se responder ou se abster corresponde a `expected_behavior`. |
| `factual_correctness` | Se todos os fatos esperados aparecem na resposta. |
| `retrieval_relevance` | Se uma evidência de referência foi recuperada. |
| `citation_validity` | Se a resposta cita uma fonte de referência sem erro de grounding. |
| `intent_match` | Se a intenção prevista corresponde a `expected_intent`. |
| `grounded_rate` | Diagnóstico do validador de grounding do fluxo, separado da correção factual. |
| `abstain_rate` | Frequência de respostas que indicam falta de evidência. |
| `avg_score` | Pontuação média apenas dos casos com critérios suficientes. |

O relatório informa, para cada métrica, quantos casos foram avaliados, aprovados,
reprovados ou ficaram sem avaliação. Uma métrica sem referência não é convertida em
sucesso. Em particular, respostas que exigem fatos e não possuem `expected_facts`
continuam executáveis, mas não recebem pontuação composta.

## Contrato dos casos

Os campos existentes continuam válidos. Casos novos podem acrescentar:

```json
{
  "expected_facts": [
    "frase obrigatória",
    {"id": "fact-2", "accepted_phrases": ["forma aceita A", "forma aceita B"]}
  ],
  "reference_evidence": [
    {"source": "documento.md", "contains": ["termo obrigatório no chunk"]}
  ]
}
```

Cada fato passa quando uma de suas frases aceitas ocorre na resposta normalizada; todos
os fatos do caso são obrigatórios. As entradas de `reference_evidence` são alternativas:
basta recuperar uma delas. `source` valida o nome do arquivo e `contains`, quando
informado, valida o conteúdo recuperado. Em casos `no_answer`, fatos e citações ficam
como não aplicáveis quando não foram informados.

Para validar somente o avaliador com a fixture sintética, sem serviços externos:

```sh
python -m unittest tests.test_offline_eval
```

Compare execuções feitas com os mesmos cenários antes de publicar mudanças na recuperação, nos modelos ou na documentação consultada. Os relatórios gerados ficam em `evaluation/reports/` e são ignorados pelo Git.
