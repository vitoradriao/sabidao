# BM-35 — baseline operacional

## Decisão

O baseline foi executado com o holdout completo e o resultado foi **reprovado**.
A issue não deve ser marcada como aprovada: o relatório registra uma falha crítica
de respostas sem suporte e falhas adicionais de correção factual, esclarecimento e
recall.

Relatório sanitizado e versionado:

- [baseline-operacional-20260921.json](baseline-operacional-20260921.json)

## Escopo executado

- Commit avaliado: `7fe4ed0f17498c37a6b80367dce0df4a0902b424`.
- Dataset: 30 casos, sendo 20 de desenvolvimento e 10 de holdout.
- Revisão humana: `vitoradriao`, 21/09/2026; 30 casos aprovados, sem divergências registradas.
- Corpus: 27 documentos, 5.108 seções e 5.502 chunks, com identidade vetorial registrada.
- Desenvolvimento executado antes do congelamento; evidências foram alinhadas aos arquivos canônicos.
- Holdout executado integralmente com `--gate`; o comando retornou código 1 conforme esperado para reprovação.
- Execuções do avaliador foram `dry-run`, sem gravar resultados nas tabelas de avaliação.

## Resultado do holdout

O holdout avaliou os 10 casos previstos. O critério crítico
`critical-no-unsupported-answer` falhou com taxa de 25%. Também falharam:

- correção factual: 50% (limiar 70%);
- esclarecimentos necessários: 0% (limiar 80%);
- Recall@20: 75% (limiar 80%).

Passaram cobertura de perguntas respondíveis, abstenções corretas e ausência de
falsas alegações de ausência. O status final é `failed`, não `incomplete`: todos os
casos e populações do holdout foram executados.

## Próximo passo do roadmap

Corrigir primeiro as respostas sem suporte e o comportamento de esclarecimento no
conjunto de desenvolvimento. Recalibrar e congelar novamente; só então executar um
novo holdout. Não ajustar a solução com base nos resultados do holdout atual.

O custo permanece parcialmente incompleto porque o provider de embeddings não
retornou uso/preço. O backup pré-reindexação está em
`runtime/issue53_pre_reindex_20260921.dump` e não deve ser versionado ou publicado.
