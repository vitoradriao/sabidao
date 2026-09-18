# Banco de dados e dimensões dos embeddings

[Documentação](../docs/README.md) / SQL

O esquema precisa corresponder à dimensão produzida pelo modelo de embeddings e ao valor de `EMBEDDING_DIMENSIONS` no `.env`.

| Arquivo | Perfil |
| --- | --- |
| `setup_1536.sql` | Vetores de 1536 dimensões, padrão da configuração e do Docker. |
| `setup_3072.sql` | Vetores de 3072 dimensões, para avaliação separada conforme o suporte do ambiente. |

Os scripts de configuração recriam estruturas da base. Revise o SQL e faça uma cópia de segurança antes de aplicá-los a um banco com dados.

## Configuração

Para o perfil padrão:

```env
EMBEDDING_DIMENSIONS=1536
```

Para o perfil de 3072 dimensões, use o esquema correspondente e configure:

```env
EMBEDDING_DIMENSIONS=3072
```

Depois de uma mudança de modelo ou dimensão, reingira os documentos para gerar embeddings compatíveis. Se o ambiente não aceitar o perfil de 3072 dimensões, utilize o perfil de 1536.

## Migrações disponíveis

| Arquivo | Finalidade |
| --- | --- |
| `migrate_hybrid_search.sql` | Busca híbrida pela função `hybrid_match_chunks`. |
| `add_knowledge_gaps.sql` | Registro e consulta de lacunas de conhecimento. |
| `add_feedback_memory.sql` | Proposta, revisão, publicação e consulta de correções. |
| `add_evaluation_tables.sql` | Tabelas `evaluation_runs`, `evaluation_results` e visão `evaluation_run_summary`. |
| `add_analytical_context.sql` | Seções de documentos e metadados dos trechos. |
| `migrate_priority.sql` | Priorização de documentos. |
| `add_section_retrieval_1536.sql` | Consulta de seções com embeddings de 1536 dimensões. |
| `add_section_retrieval_3072.sql` | Consulta de seções com embeddings de 3072 dimensões. |

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

Em bases existentes, reaplique o arquivo `add_section_retrieval_1536.sql` ou
`add_section_retrieval_3072.sql` correspondente à dimensão configurada. A migração
recria somente as funções e os índices/colunas aditivos; não remove tabelas nem exige
reindexação dos documentos.

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

A inicialização não é repetida em volumes existentes. Para atualizá-los, revise as migrações aplicáveis em um ambiente de testes antes da implantação.
