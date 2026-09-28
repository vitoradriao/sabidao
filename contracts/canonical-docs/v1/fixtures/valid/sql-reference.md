---
schema_version: 1.0.0
document_id: 10000000-0000-4000-8000-000000000005
revision: 1
title: Consultas sintéticas de pedido
language: pt-BR
doc_type: md
semantic_type: sql_reference
products: [winthor]
taxonomy:
  taxonomy_version: taxonomy_v1
  primary_module: sql_integracao
  secondary_modules: [pedidos_vendas]
  answer_mode: sql_lookup
aliases: []
sources:
  - source_id: repositorio-sql
    kind: repository
    locator: fontes/consultas-pedido.sql
    source_version: fedcba9876543210fedcba9876543210fedcba98
    sha256: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
    notes: null
applicability:
  status: known
  product_versions:
    - product: winthor
      expression: rotina compatível conforme fonte
  erp_systems: [winthor]
  valid_from: null
  valid_until: null
  notes: Sem intervalo de datas declarado.
review:
  status: reviewed
  reviewer: DBA de suporte
  reviewed_at: "2026-09-21T12:30:00-03:00"
  notes: SQL sintético, sem dados de cliente.
sections:
  localizar-pedido:
    title: Localizar pedido
    heading_level: 2
    source_refs:
      - source_id: repositorio-sql
        locator: {kind: line_range, start: 1, end: 12}
    classification_override: null
    summary: A consulta e seus cuidados permanecem no corpo Markdown.
    entities: {tables: [PCPEDC], fields: [NUMPED]}
---

# Consultas sintéticas de pedido

## Localizar pedido {#localizar-pedido}

```sql
SELECT NUMPED
FROM PCPEDC
WHERE NUMPED = :numero_sintetico;
```

O SQL é apenas estrutural e não deve ser executado.
