---
schema_version: 1.0.0
document_id: 30000000-0000-4000-8000-000000000002
revision: 1
title: Fixture com referência inválida
language: pt-BR
doc_type: md
semantic_type: guide
products: [maxpedido]
taxonomy:
  taxonomy_version: fixture_v1
  primary_module: configuracao
  secondary_modules: []
  answer_mode: reference
aliases: []
sources:
  - source_id: fonte-existente
    kind: unknown
    locator: null
    source_version: null
    sha256: null
    notes: Origem indisponível.
applicability:
  status: unknown
  product_versions: []
  erp_systems: []
  valid_from: null
  valid_until: null
  notes: Sem aplicabilidade conhecida.
review: {status: pending, reviewer: null, reviewed_at: null, notes: null}
sections:
  secao-invalida:
    title: Seção inválida
    heading_level: 2
    source_refs:
      - source_id: fonte-inexistente
        locator: {kind: unknown, reason: Referência criada para testar a rejeição.}
    classification_override: null
    summary: null
    entities: {}
---

# Fixture com referência inválida

## Seção inválida {#secao-invalida}

Esta seção aponta deliberadamente para uma fonte inexistente.
