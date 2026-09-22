---
schema_version: 1.0.0
document_id: 10000000-0000-4000-8000-000000000004
revision: 1
title: Diagnosticar falha de sincronização
language: pt-BR
doc_type: md
semantic_type: troubleshooting
products: [maxpedido]
taxonomy:
  taxonomy_version: taxonomy_v1
  primary_module: pedidos_vendas
  secondary_modules: [sql_integracao]
  answer_mode: troubleshooting
aliases: []
sources:
  - source_id: artigo-suporte
    kind: url
    locator: https://example.invalid/artigo/123
    source_version: "2026-09-01"
    sha256: null
    notes: URL deliberadamente não resolvível; fixture offline.
applicability:
  status: unknown
  product_versions: []
  erp_systems: []
  valid_from: null
  valid_until: null
  notes: A fonte não declara versões afetadas.
review: {status: pending, reviewer: null, reviewed_at: null, notes: null}
sections:
  sintoma-pedido-pendente:
    title: Pedido permanece pendente
    heading_level: 2
    source_refs:
      - source_id: artigo-suporte
        locator: {kind: url_fragment, value: "#pedido-pendente"}
    classification_override: {answer_mode: troubleshooting}
    summary: Sintoma, evidência e ação corretiva ficam no corpo Markdown.
    entities: {statuses: [PENDENTE], error_terms: [Falha de sincronização]}
---

# Diagnosticar falha de sincronização

## Pedido permanece pendente {#sintoma-pedido-pendente}

- Sintoma sintético: o pedido permanece com status `PENDENTE`.
- Evidência: não definida nesta fixture.
- Ação: deve ser confirmada em uma fonte antes de publicação.
