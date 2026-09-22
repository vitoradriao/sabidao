---
schema_version: 1.0.0
document_id: 10000000-0000-4000-8000-000000000003
revision: 1
title: Dicionário sintético de dados
language: pt-BR
doc_type: md
semantic_type: data_dictionary
products: [winthor]
taxonomy:
  taxonomy_version: taxonomy_v1
  primary_module: sql_integracao
  secondary_modules: []
  answer_mode: reference
aliases: []
sources:
  - source_id: planilha-dicionario
    kind: file
    locator: fontes/dicionario.xlsx
    source_version: null
    sha256: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
    notes: null
applicability:
  status: unknown
  product_versions: []
  erp_systems: []
  valid_from: null
  valid_until: null
  notes: Sem versão informada.
review: {status: pending, reviewer: null, reviewed_at: null, notes: null}
sections:
  tabela-pctabpr:
    title: Tabela PCTABPR
    heading_level: 2
    source_refs:
      - source_id: planilha-dicionario
        locator: {kind: table, value: PCTABPR}
    classification_override: null
    summary: Campos sintéticos da tabela.
    entities: {tables: [PCTABPR], fields: [NUMREGIAO, CODPROD]}
---

# Dicionário sintético de dados

## Tabela PCTABPR {#tabela-pctabpr}

| Campo | Tipo | Chave | Significado |
| --- | --- | --- | --- |
| `NUMREGIAO` | Sintético | Não informado | Exemplo sem regra operacional |
| `CODPROD` | Sintético | Não informado | Exemplo sem regra operacional |
