---
schema_version: 1.0.0
document_id: 10000000-0000-4000-8000-000000000001
revision: 2
title: Configurar conta corrente
language: pt-BR
doc_type: md
semantic_type: procedure
products: [maxpedido]
taxonomy:
  taxonomy_version: fixture_v1
  primary_module: configuracao
  secondary_modules: [financeiro]
  answer_mode: procedure
aliases: [Configuração de CC]
sources:
  - source_id: manual-conta-corrente
    kind: repository
    locator: fontes/manual-conta-corrente.md
    source_version: 0123456789abcdef0123456789abcdef01234567
    sha256: null
    notes: null
applicability:
  status: known
  product_versions:
    - product: maxpedido
      expression: ">= 5.0"
  erp_systems: [winthor]
  valid_from: "2026-01-01"
  valid_until: null
  notes: Vigência declarada apenas para o produto informado.
review:
  status: reviewed
  reviewer: Equipe de suporte
  reviewed_at: "2026-09-21T12:00:00Z"
  notes: Revisão humana da fixture.
sections:
  habilitar-conta-corrente:
    title: Habilitar conta corrente
    heading_level: 2
    source_refs:
      - source_id: manual-conta-corrente
        locator: {kind: line_range, start: 10, end: 28}
    classification_override: null
    summary: Procedimento sintético para habilitar o recurso.
    entities: {parameters: [CON_USACREDRCA], tables: [MXSPARAMETRO]}
  acessar-configuracao:
    title: Acessar a configuração
    heading_level: 3
    source_refs:
      - source_id: manual-conta-corrente
        locator: {kind: line_range, start: 12, end: 14}
    classification_override: null
    summary: null
    entities: {}
  localizar-parametro:
    title: Localizar o parâmetro
    heading_level: 4
    source_refs:
      - source_id: manual-conta-corrente
        locator: {kind: line_range, start: 15, end: 18}
    classification_override: null
    summary: null
    entities: {parameters: [CON_USACREDRCA]}
  confirmar-valor:
    title: Confirmar o valor
    heading_level: 5
    source_refs:
      - source_id: manual-conta-corrente
        locator: {kind: line_range, start: 19, end: 22}
    classification_override: null
    summary: null
    entities: {}
  resultado-esperado:
    title: Resultado esperado
    heading_level: 6
    source_refs:
      - source_id: manual-conta-corrente
        locator: {kind: line_range, start: 23, end: 28}
    classification_override: null
    summary: null
    entities: {}
---

# Configurar conta corrente

## Habilitar conta corrente {#habilitar-conta-corrente}

Este é um procedimento sintético, sem instruções operacionais reais.

### Acessar a configuração {#acessar-configuracao}

A fixture reserva este nível para demonstrar a hierarquia.

#### Localizar o parâmetro {#localizar-parametro}

O identificador sintético é `CON_USACREDRCA`.

##### Confirmar o valor {#confirmar-valor}

O valor real deve ser obtido da fonte antes de uma migração editorial.

###### Resultado esperado {#resultado-esperado}

O resultado desta fixture é somente validar headings H2–H6.
