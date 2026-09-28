---
schema_version: 1.0.0
document_id: 10000000-0000-4000-8000-000000000069
revision: 1
title: Guia canônico sintético
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
  - source_id: original
    kind: repository
    locator: source.md
    source_version: fedcba9876543210fedcba9876543210fedcba98
    sha256: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
    notes: null
applicability:
  status: known
  product_versions:
    - product: winthor
      expression: exemplo sintético
  erp_systems: [winthor]
  valid_from: null
  valid_until: null
  notes: Exemplo para teste offline.
review:
  status: reviewed
  reviewer: Pessoa revisora sintética
  reviewed_at: "2026-09-22T12:00:00-03:00"
  notes: Conteúdo sintético.
sections:
  consulta:
    title: Consulta
    heading_level: 2
    source_refs:
      - source_id: original
        locator: {kind: line_range, start: 1, end: 18}
    classification_override: null
    summary: Consulta sintética para verificar preservação.
    entities: {tables: [PCPEDC], fields: [NUMPED]}
---

# Guia canônico sintético

## Consulta {#consulta}

Se FLAG_X=1, não execute `--force`.
Exceto em homologação, confirme o código PED-123.
- Mantenha o campo NUMPED na consulta.

| Campo | Valor |
| --- | --- |
| FLAG_X | ativo |

[Manual](https://example.invalid/manual)
![Fluxo](figure.png)

```sql
SELECT NUMPED FROM PCPEDC WHERE FLAG_X = 1;
```
