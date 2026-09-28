# Taxonomia documental v1

[Documentação](README.md) / Taxonomia documental

O registro [vocabulary.json](../contracts/canonical-docs/v1/vocabulary.json) define os
códigos aceitos na classificação editorial `taxonomy_v1`. `doc_type` continua
indicando o formato físico, como `md`; não é o assunto nem o tipo semântico do
documento. Uma referência a uma tabela, campo ou endpoint é uma entidade
mencionada e, por si só, não transforma uma ficha de configuração em SQL.

## Módulos

| Código | Assunto principal | Exemplo |
| --- | --- | --- |
| `configuracao` | Parâmetros, permissões e ajustes de produto | Uma ficha `CON_USACREDRCA` que informa a tabela `MXSPARAMETRO` |
| `pedidos_vendas` | Pedido, orçamento, venda e campanhas comerciais | Desconto progressivo em pedido |
| `logistica` | Rotas, visitas, entregas e aplicativo de motorista | Sincronização de roteiro de visitas |
| `financeiro` | Pagamentos, crédito, títulos e conta corrente | Saldo de conta corrente |
| `sql_integracao` | Consulta SQL, dicionário técnico, APIs e integração cuja finalidade é técnica | Uma consulta de diagnóstico com `SELECT` |
| `unknown` | Assunto não confirmado pela fonte nem pelas regras determinísticas | Documento sem contexto suficiente |

O autor escolhe um módulo primário pelo objetivo da seção. Módulos secundários
registram relações úteis sem substituir esse objetivo. Uma ficha de parâmetro
que menciona uma tabela pode ter `configuracao` como primário e
`sql_integracao` como secundário. `unknown` é incerteza explícita, não uma
categoria genérica para evitar revisão.

## Modos de resposta

| Código | Tipo de resposta esperado | Exemplo |
| --- | --- | --- |
| `procedure` | Passos para executar uma tarefa | Como configurar uma rotina |
| `configuration` | Valor, efeito ou escopo de um ajuste | O que controla `CON_USACREDRCA` |
| `reference` | Definição ou relação de referência | Significado de um campo |
| `troubleshooting` | Diagnóstico de sintoma e ação fundamentada | Erro de sincronização |
| `sql_lookup` | SQL ou consulta técnica como resposta principal | Consulta para localizar um registro |
| `unknown` | Forma de resposta não determinada | Seção incompleta |

Uma ficha que contém `Tabela: MXSPARAMETRO` continua em `configuration` quando
seu objetivo é explicar um parâmetro. `sql_lookup` exige que a consulta ou o
dicionário SQL sejam a finalidade da seção. O tipo semântico do documento
(`semantic_type`) descreve seu perfil editorial e não substitui o modo de
resposta da seção.

## Precedência e auditoria

O documento canônico declara `taxonomy.taxonomy_version`, módulo primário,
módulos secundários e modo de resposta. Cada seção herda esses valores.
`classification_override` substitui somente os campos presentes; `null`
significa herança integral. A classificação editorial explícita prevalece
sobre inferências. A chave estável da seção preserva essa decisão se o título
ou o caminho do arquivo mudar.

Fontes legadas sem front matter são classificadas por regras determinísticas.
Sinais no título e nos headings indicam o assunto; referências incidentais no
corpo, como `Tabela: MXSPARAMETRO`, ficam nas entidades. Quando o sinal local é
insuficiente, a seção herda o assunto do documento ou do heading pai. A versão
da regra, o motivo e os sinais são metadados derivados, separados dos valores
editoriais. Uma mudança de regra requer nova projeção; o histórico do corpus
já indexado não é reclassificado automaticamente.

## Compatibilidade com a busca existente

A recuperação ainda filtra os códigos legados de `module`. A ingestão conserva
esse campo e armazena a classificação canônica separadamente durante a
transição:

| Canônico | `module` de busca |
| --- | --- |
| `configuracao` | `parametros_configuracao` |
| `pedidos_vendas` | `pedidos_vendas` |
| `logistica` | `rotas_visitas_consultas` |
| `financeiro` | `financeiro_pagamentos` |
| `sql_integracao` | `sql_integracao` |
| `unknown` | `geral` |

Uma classificação secundária não altera automaticamente o filtro primário.
Categorias legadas mais específicas, como `campanhas_descontos`,
`suporte_processos`, `gestao_operacional` e `glossario`, continuam no campo de
busca enquanto nenhum sinal local forte justifica sua substituição. O módulo
canônico correspondente fica no metadado; esta preservação evita perder
resultados em consultas que ainda filtram os códigos antigos.
O corpus previamente indexado pode conter classificações de versões anteriores;
compare o relatório de migração e a recuperação antes de promover um lote.
O contrato de projeção e identidade canônica no banco é tratado separadamente
na issue #67.

Os modos também preservam a compatibilidade: `procedure` é gravado como
`process`, `configuration` como `configuration`, `reference` e `unknown` como
`general`, `troubleshooting` como `troubleshooting` e `sql_lookup` como
`sql_lookup`. Os modos legados `integration` e `process` são normalizados no
metadado canônico para `reference` e `procedure`, respectivamente.

## Conferência antes de migrar

No Python 3.11, o relatório offline compara a classificação existente com a
proposta sem consultar banco, modelos ou embeddings:

```powershell
py -3.11 -B scripts/taxonomy_dry_run.py --root documentos --sample-size 20 --output runtime/taxonomy-dry-run.json
```

Revise as contagens, as mudanças por documento e as amostras. O relatório não
altera o índice. Para descartar a proposta, remova o relatório; se a nova
classificação for aplicada futuramente, a reversão dependerá do snapshot e do
plano de promoção desse lote. Não execute reingestão com `--force` como parte
desta conferência.
