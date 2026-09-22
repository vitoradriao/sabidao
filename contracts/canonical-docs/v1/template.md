---
schema_version: 1.0.0
# Gere um UUID novo; nunca reutilize o valor de exemplo abaixo.
document_id: 00000000-0000-4000-8000-000000000001
revision: 1
title: Título do documento
language: pt-BR
doc_type: md
semantic_type: procedure
products:
  - maxpedido
taxonomy:
  taxonomy_version: taxonomy_v1
  primary_module: configuracao
  secondary_modules: []
  answer_mode: procedure
aliases: []
sources:
  - source_id: fonte-principal
    kind: repository
    locator: caminho/da/fonte-original.md
    source_version: commit-ou-versao
    sha256: null
    notes: null
applicability:
  status: unknown
  product_versions: []
  erp_systems: []
  valid_from: null
  valid_until: null
  notes: A fonte não informa versões de produto ou período de validade.
review:
  status: pending
  reviewer: null
  reviewed_at: null
  notes: null
sections:
  pre-requisitos:
    title: Pré-requisitos
    heading_level: 2
    source_refs:
      - source_id: fonte-principal
        locator:
          kind: line_range
          start: 1
          end: 10
    classification_override: null
    summary: null
    entities: {}
  procedimento:
    title: Procedimento
    heading_level: 2
    source_refs:
      - source_id: fonte-principal
        locator:
          kind: line_range
          start: 11
          end: 30
    classification_override: null
    summary: null
    entities: {}
  resultado:
    title: Resultado esperado
    heading_level: 2
    source_refs:
      - source_id: fonte-principal
        locator:
          kind: line_range
          start: 31
          end: 35
    classification_override: null
    summary: null
    entities: {}
---

# Título do documento

## Pré-requisitos {#pre-requisitos}

Descreva somente os pré-requisitos confirmados pela fonte.

## Procedimento {#procedimento}

1. Descreva a ação.
2. Preserve nomes de telas, campos, parâmetros e valores.

## Resultado esperado {#resultado}

Descreva o resultado observável e as exceções confirmadas.
