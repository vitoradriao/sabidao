# Benchmark de contextualização — issue #16

Data: 21/09/2026. Escopo autorizado: um documento, 20 casos de desenvolvimento,
DeepSeek para texto e Gemini para embeddings. O holdout não foi executado.

## Decisão

**Manter o contexto determinístico.** A contextualização por LLM não deve virar o
default do roadmap. Ela reduziu Recall@10, Recall@20 e nDCG@10, triplicou a taxa de
alegação falsa de ausência, aumentou o tempo de ingestão em 213,4% e acrescentou
custo. Os ganhos observados em comportamento e esclarecimento não compensam a piora
do retrieval. O resultado vale para esta amostra pequena e ainda não revisada por
especialista.

A decisão foi aplicada ao produto com `CONTEXTUAL_RETRIEVAL_ENABLED=false` no
default interno e no `.env.example`. O mecanismo por LLM permanece disponível
somente por ativação explícita para permitir novos experimentos controlados.

## Identidade do experimento

- commit: `c1a5e721ab41183f2ebcabbae8f1a061602064f0`;
- dataset: `01e363b8f1f9acc7590fbf22b0be865418d394984501275cb8fe50cc28d71a89`;
- política: `3116a568bb59671056b7890f03651ca9dcfe46fe5c589ec0396cbea382785118`;
- corpus autorizado: 1 arquivo, fingerprint `3188ceea1531a572bc222212fc83f834eeb6f5bb20cee30094faa0f104b51049`;
- geração, reformulação e reranking: DeepSeek `deepseek-flash`;
- embeddings: Gemini `gemini-embedding-001`, 1536 dimensões, `rag-text-v1`;
- 132 chunks e 124 seções por variante;
- comparação de runtime: `compatible_with_declared_changes`; somente a flag de
  contextualização e o fingerprint do índice resultante diferiram deliberadamente.

## Métricas

| Métrica | A — determinística | B — LLM | Diferença absoluta | Diferença relativa |
| --- | ---: | ---: | ---: | ---: |
| Recall@10 | 64,29% | 57,14% | -7,15 p.p. | -11,12% |
| Recall@20 | 85,71% | 71,43% | -14,28 p.p. | -16,66% |
| nDCG@10 | 64,29% | 57,14% | -7,15 p.p. | -11,12% |
| Correção factual | 85,71% | 85,71% | 0 p.p. | 0% |
| Alegações sem suporte | 5,56% (n=18) | 0% (n=17) | -5,56 p.p. | não comparável diretamente |
| Validade de citações | 100% | 100% | 0 p.p. | 0% |
| Comportamento esperado | 80% | 85% | +5 p.p. | +6,25% |
| Alegação falsa de ausência | 7,14% | 21,43% | +14,29 p.p. | +200,14% |
| Esclarecimento necessário | 0% | 25% | +25 p.p. | indisponível |
| Abstenção correta | 100% | 100% | 0 p.p. | 0% |
| Latência p50 | 6.983 ms | 6.537 ms | -446 ms | -6,39% |
| Latência p95 | 15.837 ms | 13.727 ms | -2.110 ms | -13,32% |
| Ingestão | 21.372 ms | 66.980 ms | +45.608 ms | +213,40% |

## Uso e custo

Na ingestão, A fez zero chamadas de contextualização. B fez 7 chamadas, consumiu
166.507 tokens e contextualizou 131 de 132 chunks; um chunk usou fallback. O custo
DeepSeek da ingestão B foi estimado em US$ 0,014692 pelas tarifas off-peak vigentes.

Na avaliação final, A consumiu 146.796 tokens em 41 chamadas de modelo, com custo
DeepSeek estimado em US$ 0,018940. B consumiu 145.156 tokens em 40 chamadas, com
custo estimado em US$ 0,026226. O custo final atribuído às variantes, sem embeddings,
foi US$ 0,018940 para A e US$ 0,040918 para B: acréscimo de US$ 0,021978 (+116,05%).

Incluindo a execução A anterior preservada e todas as tentativas de ingestão, o gasto
DeepSeek estimado foi US$ 0,087373. O Gemini não expôs tokens por chamada; por isso o
custo total é incompleto. Usando uma aproximação conservadora de um token por caractere
e a tarifa paga de US$ 0,15/M tokens, o custo total de Gemini fica abaixo de US$ 0,133549.
Assim, o teto conservador do experimento completo é US$ 0,220922, abaixo do orçamento
autorizado de US$ 0,50. Custo desconhecido não foi contabilizado como zero.

## Falhas e limitações

- os 20 casos têm `human_review: pending`; revisão automática não é revisão humana;
- o holdout não foi autorizado e permaneceu intocado;
- a amostra tem somente 14 casos respondíveis, 4 ambíguos e 2 sem evidência;
- B teve um fallback parcial de contextualização e uma resposta vazia;
- `unsupported_claims` tem denominadores diferentes, portanto sua melhora isolada não
  sustenta adoção;
- duas bases inicialmente reservadas receberam gravações externas. Esses resultados
  foram rejeitados; a execução válida usou bancos adicionais exclusivos e vazios;
- nenhum banco ou volume de produção foi alterado.

## Comandos e saídas

Os comandos foram executados em contêineres isolados com o corpus montado somente para
leitura. Principais resultados:

| Comando | Saída |
| --- | ---: |
| `python -m unittest tests.test_contextual_ingest_benchmark tests.test_contextual_ingest_identity tests.test_offline_eval` | 0 — 38 testes OK |
| migrações SQL nos bancos exclusivos A e B | 0 — 12/12 em cada banco |
| runner de ingestão A | 0 |
| avaliador A, `--dry-run --split development` | 0 |
| runner de ingestão B com relatório A como referência | 0 |
| avaliador B com comparação de identidade | 0 — `compatible_with_declared_changes` |

Evidência de isolamento: os bancos válidos foram `issue16_a_20260921_1538` e
`issue16_b_20260921_1538`, nos volumes `sabidao_issue16_a_data` e
`sabidao_issue16_b_data`. O contêiner operacional `bot-maxima-postgres` permaneceu em
execução com o volume `sabidao_postgres_data`, sem migração, reindexação, exclusão ou
reinicialização pelo benchmark.
