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
| `migrate_canonical_identity.sql` | Identidade editorial, revisão e metadados canônicos de documentos e chaves estáveis de seção. |
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

### Identidade editorial canônica

`migrate_canonical_identity.sql` é uma migração aditiva para o contrato
documental. Ela acrescenta, sem preencher valores existentes:

- `documents.canonical_id` (`UUID NULL`), com unicidade apenas entre valores não nulos;
- `documents.schema_version` (`TEXT NULL`), que identifica o contrato documental;
- `documents.document_revision` (`INTEGER NULL`), que registra a revisão quando conhecida;
- `documents.metadata` (`JSONB NULL`), para metadados editoriais do documento;
- `document_sections.section_key` (`TEXT NULL`), com unicidade por documento apenas entre valores não nulos.

Os dados legados continuam válidos. Quando a identidade editorial não é conhecida,
essas colunas permanecem nulas; a migração não atribui IDs, revisões ou metadados por
aproximação. A mesma `section_key` pode existir em documentos diferentes, mas não se
repete dentro do mesmo documento quando não é nula.

#### Pré-requisitos e ordem

Em uma base existente, `documents` precisa existir e
`document_sections` precisa ter sido criada por `add_analytical_context.sql`. Se o
contexto analítico ainda não estiver aplicado, faça isso antes da identidade:

```sh
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/add_analytical_context.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f sql/migrate_canonical_identity.sql
```

Se `document_sections` já existir, aplique somente a segunda linha. A migração pode
ser reaplicada: usa `IF NOT EXISTS` nas colunas e índices e não converte documentos.
Faça backup ou snapshot antes da alteração e valide primeiro em uma base descartável.

Para um banco novo, não aplique essa migração isoladamente: o bootstrap executa
`setup_1536.sql`, `add_analytical_context.sql` e, em seguida,
`migrate_canonical_identity.sql`, antes das migrações de prioridade, retrieval e
identidade de ingestão.

#### Verificar a aplicação

Depois da migração, confira apenas o catálogo do banco, sem alterar dados:

```sql
SELECT table_name, column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_schema = 'public'
  AND (
      (table_name = 'documents' AND column_name IN
          ('canonical_id', 'schema_version', 'document_revision', 'metadata'))
      OR (table_name = 'document_sections' AND column_name = 'section_key')
  )
ORDER BY table_name, column_name;

SELECT indexname, tablename, indexdef
FROM pg_indexes
WHERE schemaname = 'public'
  AND indexname IN (
      'documents_canonical_id_unique',
      'document_sections_document_section_key_unique'
  )
ORDER BY indexname;
```

As colunas devem aparecer como nullable e os dois índices como índices parciais.
Uma falha por duplicidade em valores canônicos já preenchidos deve ser investigada;
não remova dados para fazer a migração passar.

#### Limites e rollback

Esta migração não converte `documentos/`, não altera o manifesto, não ingere nem
reindexa fontes e não chama provider de embeddings. Também não altera provider,
modelo, dimensão `1536`, preprocessamento, RRF ou defaults de ranking. O preenchimento
de identidade canônica pertence à ingestão/projeção posterior; não execute
`ingest.py --force` ou `!reindex` como parte do upgrade.

Não há script down publicado. Se for necessário retornar a aplicação, restaure a
versão anterior e mantenha a migração instalada: versões que não usam essas colunas
continuam compatíveis. Se for indispensável desfazer o schema, restaure o
backup/snapshot validado ou prepare uma operação reversível específica; não use
`setup_1536.sql`, `setup_3072.sql` nem remova o volume para fazer rollback.

## Inicialização no Docker

Em um volume novo, `docker/postgres/init/00-bootstrap.sh` aplica:

1. `setup_1536.sql`
2. `migrate_hybrid_search.sql`
3. `add_knowledge_gaps.sql`
4. `add_feedback_memory.sql`
5. `add_evaluation_tables.sql`
6. `add_analytical_context.sql`
7. `migrate_canonical_identity.sql`
8. `migrate_priority.sql`
9. `add_section_retrieval_1536.sql`
10. `migrate_canonical_retrieval_1536.sql`
11. `add_embedding_index_identity.sql`
12. `add_ingest_identity.sql`
13. `add_evaluation_identity.sql`

A inicialização não é repetida em volumes existentes. Para atualizá-los, aplique as
migrações correspondentes, começando pelas dependências ausentes, em um ambiente de
testes antes da implantação. Os scripts `setup_1536.sql` e `setup_3072.sql` recriam
tabelas e são destrutivos; não são procedimentos de upgrade de uma base com dados.

## Recuperação e avaliação canônicas

Após `migrate_canonical_identity.sql` e `add_section_retrieval_1536.sql`, aplique
`migrate_canonical_retrieval_1536.sql` em bancos existentes com vetores de 1536
dimensões. Para a variante de 3072 dimensões, use
`migrate_canonical_retrieval_3072.sql` após a camada de busca correspondente.
Essas migrações substituem as funções de busca para projetar identidade,
revisão, seção e proveniência canônicas junto dos campos legados. Em seguida,
reaplique `add_evaluation_identity.sql` para atualizar o fingerprint sanitizado
do corpus. Faça snapshot e teste as consultas antes de atualizar a aplicação;
os scripts não convertem documentos nem reindexam vetores.

Uma versão anterior da aplicação pode voltar a usar as colunas adicionais. Para
desfazer funções em produção, restaure a definição de busca validada para a base
anterior; não execute `setup_1536.sql` nem `setup_3072.sql` como rollback.
