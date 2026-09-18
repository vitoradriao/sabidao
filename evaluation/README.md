# Avaliação das respostas do maxPedido

[Documentação](../docs/README.md) / Avaliação

Este diretório reúne cenários de referência e ferramentas para comparar a qualidade das respostas do Sabidão.

| Arquivo | Finalidade |
| --- | --- |
| `datasets/maxpedido_seed_cases.json` | Cenários iniciais revisados. |
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
python evaluation/run_offline_eval.py
```

Para limitar os casos e escolher o relatório:

```sh
python evaluation/run_offline_eval.py --limit 50 --output-report evaluation/reports/latest.json
```

## Métricas

| Campo | O que acompanha |
| --- | --- |
| `grounded_rate` | Fundamentação das respostas. |
| `citation_ok_rate` | Presença de citações conforme os critérios do avaliador. |
| `abstain_rate` | Frequência de respostas que indicam falta de evidência. |
| `intent_accuracy` | Acerto da classificação da intenção. |
| `avg_score` | Pontuação média dos casos. |

Compare execuções feitas com os mesmos cenários antes de publicar mudanças na recuperação, nos modelos ou na documentação consultada. Os relatórios gerados ficam em `evaluation/reports/` e são ignorados pelo Git.
