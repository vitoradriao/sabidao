# Banco de dados e dimensões dos embeddings

[Documentação](../docs/README.md) / SQL

O esquema precisa corresponder à dimensão produzida pelo modelo de embeddings e ao valor de `EMBEDDING_DIMENSIONS` no `.env`.

| Arquivo | Perfil |
| --- | --- |
| `setup_1536.sql` | Vetores de 1536 dimensões, único perfil suportado pela aplicação e pelo Docker. |
| `setup_3072.sql` | Referência histórica/experimental; não é suportado pela aplicação atual. |

Os scripts de configuração recriam estruturas da base. Revise o SQL e faça uma cópia de segurança antes de aplicá-los a um banco com dados.

## Configuração

O único perfil aceito nesta versão é:

```env
EMBEDDING_DIMENSIONS=1536
EMBEDDING_PREPROCESSING_VERSION=rag-text-v1
```

O perfil `VECTOR(3072)` excede o limite de 2000 dimensões do índice HNSW para
`VECTOR`, e a memória de feedback permanece em 1536 dimensões. Por isso,
`EMBEDDING_DIMENSIONS=3072` falha na
inicialização. Não execute os scripts 3072 em ambientes operacionais.

Provider, modelo, dimensão e versão de preprocessamento formam uma identidade
única. Consulte [Identidade do índice vetorial](../docs/identidade-indice-vetorial.md)
antes de mudar qualquer um desses valores.

## Migrações disponíveis

| Arquivo | Finalidade |
| --- | --- |
| `migrate_hybrid_search.sql` | Busca híbrida pela função `hybrid_match_chunks`. |
| `add_knowledge_gaps.sql` | Registro e consulta de lacunas de conhecimento. |
| `add_feedback_memory.sql` | Proposta, revisão, publicação e consulta de correções. |
| `add_evaluation_tables.sql` | Tabelas `evaluation_runs`, `evaluation_results` e visão `evaluation_run_summary`. |
| `migrate_evaluation_metrics_v2.sql` | Migra avaliações existentes para métricas factuais, retrieval e citação com estado não avaliado. |
| `add_analytical_context.sql` | Seções de documentos e metadados dos trechos. |
| `migrate_priority.sql` | Priorização de documentos. |
| `add_section_retrieval_1536.sql` | Consulta de seções e texto de retrieval dos chunks com embeddings de 1536 dimensões. |
| `add_section_retrieval_3072.sql` | Referência histórica/experimental não suportada no runtime atual. |
| `add_embedding_index_identity.sql` | Registra e valida a identidade vetorial de corpus, seções e feedback. |
| `add_ingest_identity.sql` | Adiciona hashes de conteúdo e preprocessamento para ingestão incremental. |
| `add_evaluation_identity.sql` | Calcula no banco fingerprints sanitizados do corpus, feedback elegível e identidades vetoriais para avaliações. |

### Contrato da busca híbrida

As funções `hybrid_match_chunks` e `hybrid_match_sections` mantêm `similarity`
como alias compatível da similaridade vetorial medida. Esse valor pode ser usado pelos
limiares de confiança, mas não representa o ranking híbrido.

O ranking e sua proveniência são retornados separadamente:

| Campo | Significado |
| --- | --- |
| `vector_similarity` | Similaridade de cosseno medida para o candidato. |
| `lexical_score` | Pontuação full-text do candidato selecionado. |
| `fusion_score` | Pontuação RRF usada para selecionar e ordenar os candidatos. |
| `retrieval_origin` | `vector`, `lexical`, `hybrid` ou `neighbor`. |
| `retrieval_rank` | Posição do candidato principal no ranking RRF. |
| `is_neighbor` | Indica expansão adjacente posterior à seleção. |
| `seed_chunk_id` | Candidato principal que originou o vizinho. |

Vizinhos não herdam `fusion_score` nem similaridade do candidato principal. A aplicação
preserva a ordem devolvida pelo RRF até que um reranker seja executado; nesse caso, a
ordem do reranker prevalece até a montagem do contexto.

Em bases existentes, reaplique `add_section_retrieval_1536.sql`. A migração
adiciona `retrieval_text`, `contextualization_version` e o índice
`document_chunks_retrieval_fts_idx`. Linhas antigas recebem o conteúdo original
como fallback, sem chamadas de modelo, reingestão ou recálculo de embeddings. As
funções de busca passam a consultar esse índice; a próxima ingestão explícita
persiste o texto exato usado no embedding. Depois aplique
`add_embedding_index_identity.sql` e siga o procedimento de identificação da
base legada antes de iniciar o bot ou a ingestão. Em corpus grande, programe uma
janela para o preenchimento da coluna e a criação do índice GIN.

A fixture [hybrid_rrf_fixture.sql](../tests/postgres/hybrid_rrf_fixture.sql) valida o
contrato em PostgreSQL com pgvector. Ela deve ser executada somente em uma base de teste
que já tenha recebido `setup_1536.sql`, `add_analytical_context.sql` e
`add_section_retrieval_1536.sql`; todos os dados da fixture são revertidos ao final.

## Inicialização no Docker

Em um volume novo, `docker/postgres/init/00-bootstrap.sh` aplica:

1. `setup_1536.sql`
2. `migrate_hybrid_search.sql`
3. `add_knowledge_gaps.sql`
4. `add_feedback_memory.sql`
5. `add_evaluation_tables.sql`
6. `add_analytical_context.sql`
7. `migrate_priority.sql`
8. `add_section_retrieval_1536.sql`
9. `add_embedding_index_identity.sql`
10. `add_ingest_identity.sql`
11. `add_evaluation_identity.sql`

A inicialização não é repetida em volumes existentes. Para atualizá-los, revise as migrações aplicáveis em um ambiente de testes antes da implantação.
