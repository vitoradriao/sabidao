# Verificação de um lote de migração canônica

[Documentação](README.md) / Verificação de migração

Este procedimento confere preservação editorial e compara consultas antes e
depois de um lote. Os comandos de verificação consomem arquivos locais; a
execução que produz relatórios do RAG usa banco e providers e deve ocorrer em
ambientes isolados, com configuração, snapshot e orçamento congelados. Nenhum
documento do corpus é migrado por este procedimento.

## Inventário e relatório de preservação

Antes de converter, registre o commit completo do snapshot original e o
inventário independente `source_unit_ids`. Uma unidade é um trecho cuja
evidência e destino podem ser revisados juntos. Cada item de `units` precisa de
`unit_id`, `source`, `destination` ou exclusão, `disposition` e `review`.
`source` informa caminho POSIX relativo, SHA-256 do arquivo inteiro no commit,
linhas inclusivas, `format` (`legacy` ou `canonical`) e `revision` (`null` para
legado). `destination` informa caminho, SHA-256 do arquivo inteiro atual,
`canonical_id`, `section_key` e `locator` do tipo `line_range`, com linhas
inclusivas. O destino deve ser um documento válido do contrato canônico v1.

O relatório usa `schema_version: "1.0.0"`, `batch_id`,
`original_snapshot: {commit, sha256}`, `source_unit_ids` e `units`. Calcule o
hash do inventário com `preservation.compute_snapshot_sha256` após preencher
commit, IDs e origens. Esse hash cobre esses campos, em JSON UTF-8 com chaves
ordenadas e separadores compactos; não cobre destino ou decisões editoriais.
Guarde o relatório revisado junto da evidência do lote.

Uma exclusão exige `disposition.kind: "excluded"`, destino `null`, motivo,
revisor e instante. Uma fusão usa `kind: "merged"` e mantém destino canônico.
Cada unidade também exige `review.status: "reviewed"`, revisor e instante;
um destino canônico precisa de revisão no próprio front matter. Decisões
pendentes não aprovam o lote.

Execute a conferência sem rede, banco ou IA:

```sh
python scripts/verify_canonical_migration.py runtime/lote-relatorio.json > runtime/lote-preservacao.json
```

O comando lê a origem pelo objeto Git local e o destino no checkout atual.
Compara blocos de código, identificadores, links, itens de lista, células de
tabela, texto do corpo, negações, condições, exceções e imagens, inclusive o conteúdo de
imagens locais. Blocos de código preservam o texto com normalização de finais
de linha; o texto comum e os títulos preservam a ordem, enquanto listas,
células e cláusulas têm espaços e caixa normalizados. Uma
reescrita sem equivalência verificável pede revisão, em
vez de ser aprovada por heurística. `passed` exige 100% das unidades mapeadas,
revisão e nenhuma perda crítica. `failed` indica perda ou divergência
comprovada; `incomplete` indica evidência indisponível ou revisão pendente.
Os códigos de saída são 0, 1 e 2, respectivamente.

## Consultas e comparação do lote

Monte pelo menos três consultas por unidade: identificador, paráfrase e
condição de limite. Em `provenance.migration`, declare `batch_id`, `unit_id`,
`role` (`identifier`, `paraphrase`, `boundary`), `position` e `critical`.
Inclua assuntos críticos, trechos do meio e do fim, casos ambíguos e casos
sem evidência. Anote fatos, evidência canônica e comportamento esperado;
uma pessoa deve aprovar cada caso em `review.human_review`, registrando
`human_reviewer`, `human_reviewed_at` e `divergences`. A
[fixture sintética](../evaluation/datasets/migration_batch_synthetic_fixture.json)
exercita o formato sem representar validação operacional.

Antes de qualquer ensaio, copie a
[política modelo](../evaluation/migration_batch_policy.template.json) para um
arquivo do lote e preencha `frozen_at`, snapshots descartáveis, hashes de corpus,
o hash do snapshot original do relatório de preservação, limites de tokens,
p95 e custo, e as métricas de não regressão. Declare em
`declared_changes` cada variável experimental alterada, conforme a identidade
de experimento da [issue #52](https://github.com/vitoradriao/sabidao/issues/52).
O modelo contém campos nulos de propósito: não fornece limiares aprovados.

Produza relatórios de referência e candidato para o mesmo conjunto de casos,
com o mesmo modelo/configuração pertinente e snapshots identificados. O
[avaliador](../evaluation/README.md) pode chamar serviços pagos mesmo com
`--dry-run`. Em seguida execute somente a comparação local:

```sh
python evaluation/verify_migration_batch.py \
  --preservation-report runtime/lote-preservacao.json \
  --reference-report runtime/referencia.json \
  --candidate-report runtime/candidato.json \
  --policy runtime/politica-lote.json \
  --output runtime/lote-gate.json
```

O campo `disposable_database` e os identificadores de snapshot são declarações
do operador; o gate compara hashes, mas não consegue comprovar sozinho o
isolamento físico do banco. Registre essa confirmação no plano do ensaio.

O gate exige casos pareados e revisados, três papéis por unidade, evidência
anotada no top 20 de cada pergunta respondível, esclarecimento ou abstenção
corretos, nenhuma citação inválida nem falsa alegação de ausência, identidade
experimental compatível, limites observados e métricas agregadas sem regressão.
Custo incompleto, métrica ausente, revisão pendente ou snapshot não comprovado
geram `incomplete`; falha comprovada gera `failed`. Apenas `passed` retorna 0.
Uma fixture aprovada em teste não substitui ensaio real nem revisão humana.
