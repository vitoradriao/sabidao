-- ==============================================
-- SETUP DO SUPABASE PARA RAG COM PGVECTOR (3072)
-- Recria schema para embeddings Gemini em 3072 dimensoes.
-- Inclui busca hibrida (vetor + full-text) com RRF.
-- ==============================================

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

DROP FUNCTION IF EXISTS hybrid_match_chunks(vector, text, integer, double precision, float, float, text[], text[]);
DROP FUNCTION IF EXISTS hybrid_match_chunks(vector, text, integer, real, real, real, text[], text[]);
DROP FUNCTION IF EXISTS hybrid_match_chunks(vector, text, integer, double precision, float, float);
DROP FUNCTION IF EXISTS match_chunks(vector, integer, double precision);
DROP FUNCTION IF EXISTS match_chunks(vector, integer, real);
DROP FUNCTION IF EXISTS match_chunks(vector, integer, double precision, text[], text[]);
DROP FUNCTION IF EXISTS match_chunks(vector, integer, real, text[], text[]);
DROP FUNCTION IF EXISTS get_stats();
DROP TABLE IF EXISTS document_chunks;
DROP TABLE IF EXISTS documents;

CREATE TABLE documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    filename TEXT NOT NULL,
    title TEXT,
    source TEXT,
    doc_type TEXT,
    content_hash TEXT,
    processing_hash TEXT,
    chunk_count INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE document_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID REFERENCES documents(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    metadata JSONB DEFAULT '{}'::jsonb,
    embedding VECTOR(3072) NOT NULL,
    token_count INTEGER,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    -- Coluna full-text search gerada automaticamente (portugues)
    fts tsvector GENERATED ALWAYS AS (to_tsvector('portuguese', content)) STORED
);

CREATE UNIQUE INDEX documents_filename_unique
ON documents(filename);

CREATE INDEX document_chunks_embedding_hnsw_idx
ON document_chunks
USING hnsw (embedding vector_cosine_ops)
WITH (m = 24, ef_construction = 128);

CREATE INDEX document_chunks_document_id_idx
ON document_chunks(document_id);

CREATE INDEX document_chunks_doc_chunk_idx
ON document_chunks(document_id, chunk_index);

-- Indice GIN para full-text search rapido
CREATE INDEX document_chunks_fts_idx
ON document_chunks USING gin(fts);

-- ══════════════════════════════════════════════
-- Funcao: match_chunks (busca vetorial pura - fallback)
-- ══════════════════════════════════════════════
CREATE OR REPLACE FUNCTION match_chunks(
    query_embedding VECTOR(3072),
    match_count INT DEFAULT 8,
    match_threshold FLOAT DEFAULT 0.55,
    filter_doc_types TEXT[] DEFAULT NULL,
    filter_modules TEXT[] DEFAULT NULL
)
RETURNS TABLE (
    id UUID,
    document_id UUID,
    content TEXT,
    chunk_index INTEGER,
    metadata JSONB,
    filename TEXT,
    similarity FLOAT
)
LANGUAGE plpgsql
AS $$
DECLARE
    max_expanded INT := GREATEST(match_count * 2, match_count);
BEGIN
    PERFORM set_config('hnsw.ef_search', '100', true);

    RETURN QUERY
    WITH base_chunks AS (
        SELECT
            dc.id,
            dc.document_id,
            dc.content,
            dc.chunk_index,
            dc.metadata,
            dc.embedding,
            d.filename,
            d.doc_type
        FROM document_chunks dc
        JOIN documents d ON d.id = dc.document_id
        WHERE (
            COALESCE(array_length(filter_doc_types, 1), 0) = 0
            OR COALESCE(d.doc_type, dc.metadata->>'doc_type', '') = ANY(filter_doc_types)
        )
        AND (
            COALESCE(array_length(filter_modules, 1), 0) = 0
            OR COALESCE(dc.metadata->>'module', '') = ANY(filter_modules)
            OR NOT (dc.metadata ? 'module')
        )
    ),
    top_matches AS (
        SELECT
            bc.id,
            bc.document_id,
            bc.content,
            bc.chunk_index,
            bc.metadata,
            bc.filename,
            (1 - (bc.embedding <=> query_embedding))::FLOAT AS similarity
        FROM base_chunks bc
        WHERE 1 - (bc.embedding <=> query_embedding) >= match_threshold
        ORDER BY bc.embedding <=> query_embedding ASC
        LIMIT match_count
    ),
    neighbor_indices AS (
        SELECT DISTINCT tm.document_id AS doc_id, ni.idx
        FROM top_matches tm
        CROSS JOIN LATERAL (
            VALUES (tm.chunk_index - 1), (tm.chunk_index), (tm.chunk_index + 1)
        ) AS ni(idx)
        WHERE ni.idx >= 0
    ),
    expanded AS (
        SELECT
            dc.id,
            dc.document_id,
            dc.content,
            dc.chunk_index,
            dc.metadata,
            d.filename,
            COALESCE(
                tm.similarity,
                (
                    SELECT MAX(tm2.similarity) * 0.90
                    FROM top_matches tm2
                    WHERE tm2.document_id = dc.document_id
                      AND ABS(tm2.chunk_index - dc.chunk_index) = 1
                )
            )::FLOAT AS similarity
        FROM neighbor_indices ni
        JOIN document_chunks dc ON dc.document_id = ni.doc_id AND dc.chunk_index = ni.idx
        JOIN documents d ON d.id = dc.document_id
        LEFT JOIN top_matches tm ON tm.id = dc.id
    )
    SELECT
        f.id,
        f.document_id,
        f.content,
        f.chunk_index,
        f.metadata,
        f.filename,
        f.similarity
    FROM (
        SELECT DISTINCT ON (e.id)
            e.id,
            e.document_id,
            e.content,
            e.chunk_index,
            e.metadata,
            e.filename,
            e.similarity
        FROM expanded e
        WHERE e.similarity IS NOT NULL
        ORDER BY e.id, e.similarity DESC
    ) f
    ORDER BY f.similarity DESC
    LIMIT max_expanded;
END;
$$;

-- ══════════════════════════════════════════════
-- Funcao: hybrid_match_chunks (busca hibrida com RRF)
-- Combina busca vetorial + full-text search usando
-- Reciprocal Rank Fusion para resultados mais precisos.
-- ══════════════════════════════════════════════
CREATE OR REPLACE FUNCTION hybrid_match_chunks(
    query_embedding VECTOR(3072),
    query_text TEXT,
    match_count INT DEFAULT 8,
    match_threshold FLOAT DEFAULT 0.55,
    vector_weight FLOAT DEFAULT 0.6,
    fts_weight FLOAT DEFAULT 0.4,
    filter_doc_types TEXT[] DEFAULT NULL,
    filter_modules TEXT[] DEFAULT NULL
)
RETURNS TABLE (
    id UUID,
    document_id UUID,
    content TEXT,
    chunk_index INTEGER,
    metadata JSONB,
    filename TEXT,
    similarity FLOAT
)
LANGUAGE plpgsql
AS $$
DECLARE
    rrf_k CONSTANT INT := 60;
    fetch_limit CONSTANT INT := 30;
    max_expanded INT := GREATEST(match_count * 2, match_count);
BEGIN
    PERFORM set_config('hnsw.ef_search', '100', true);

    RETURN QUERY

    WITH base_chunks AS (
        SELECT
            dc.id,
            dc.document_id,
            dc.content,
            dc.chunk_index,
            dc.metadata,
            dc.embedding,
            dc.fts,
            d.filename,
            d.doc_type
        FROM document_chunks dc
        JOIN documents d ON d.id = dc.document_id
        WHERE (
            COALESCE(array_length(filter_doc_types, 1), 0) = 0
            OR COALESCE(d.doc_type, dc.metadata->>'doc_type', '') = ANY(filter_doc_types)
        )
        AND (
            COALESCE(array_length(filter_modules, 1), 0) = 0
            OR COALESCE(dc.metadata->>'module', '') = ANY(filter_modules)
            OR NOT (dc.metadata ? 'module')
        )
    ),

    vector_results AS (
        SELECT
            bc.id,
            ROW_NUMBER() OVER (ORDER BY bc.embedding <=> query_embedding ASC) AS rank_pos,
            (1 - (bc.embedding <=> query_embedding))::FLOAT AS vec_similarity
        FROM base_chunks bc
        WHERE 1 - (bc.embedding <=> query_embedding) >= match_threshold * 0.7
        ORDER BY bc.embedding <=> query_embedding ASC
        LIMIT fetch_limit
    ),

    fts_results AS (
        SELECT
            bc.id,
            ROW_NUMBER() OVER (ORDER BY ts_rank_cd(bc.fts, websearch_to_tsquery('portuguese', query_text)) DESC) AS rank_pos,
            ts_rank_cd(bc.fts, websearch_to_tsquery('portuguese', query_text))::FLOAT AS fts_rank
        FROM base_chunks bc
        WHERE bc.fts @@ websearch_to_tsquery('portuguese', query_text)
        ORDER BY ts_rank_cd(bc.fts, websearch_to_tsquery('portuguese', query_text)) DESC
        LIMIT fetch_limit
    ),

    combined AS (
        SELECT
            COALESCE(vr.id, fr.id) AS chunk_id,
            COALESCE(vector_weight * (1.0 / (rrf_k + vr.rank_pos)), 0) +
            COALESCE(fts_weight * (1.0 / (rrf_k + fr.rank_pos)), 0) AS rrf_score,
            vr.vec_similarity::FLOAT AS vec_similarity,
            (vr.id IS NOT NULL AND fr.id IS NOT NULL) AS in_both
        FROM vector_results vr
        FULL OUTER JOIN fts_results fr ON vr.id = fr.id
    ),

    top_matches AS (
        SELECT
            c.chunk_id,
            c.rrf_score,
            c.vec_similarity
        FROM combined c
        WHERE c.vec_similarity >= match_threshold * 0.5
           OR c.in_both
        ORDER BY c.rrf_score DESC
        LIMIT match_count
    ),

    neighbor_indices AS (
        SELECT DISTINCT bc.document_id AS doc_id, ni.idx
        FROM top_matches tm
        JOIN base_chunks bc ON bc.id = tm.chunk_id
        CROSS JOIN LATERAL (
            VALUES (bc.chunk_index - 1), (bc.chunk_index), (bc.chunk_index + 1)
        ) AS ni(idx)
        WHERE ni.idx >= 0
    ),

    expanded AS (
        SELECT
            dc.id,
            dc.document_id,
            dc.content,
            dc.chunk_index,
            dc.metadata,
            d.filename,
            COALESCE(
                tm.vec_similarity,
                (
                    SELECT MAX(tm2.vec_similarity) * 0.90
                    FROM top_matches tm2
                    JOIN document_chunks dc2 ON dc2.id = tm2.chunk_id
                    WHERE dc2.document_id = dc.document_id
                      AND ABS(dc2.chunk_index - dc.chunk_index) = 1
                )
            )::FLOAT AS similarity
        FROM neighbor_indices ni
        JOIN document_chunks dc ON dc.document_id = ni.doc_id AND dc.chunk_index = ni.idx
        JOIN documents d ON d.id = dc.document_id
        LEFT JOIN top_matches tm ON tm.chunk_id = dc.id
    )

    SELECT
        f.id,
        f.document_id,
        f.content,
        f.chunk_index,
        f.metadata,
        f.filename,
        f.similarity
    FROM (
        SELECT DISTINCT ON (e.id)
            e.id,
            e.document_id,
            e.content,
            e.chunk_index,
            e.metadata,
            e.filename,
            e.similarity
        FROM expanded e
        WHERE e.similarity IS NOT NULL
        ORDER BY e.id, e.similarity DESC
    ) f
    ORDER BY f.similarity DESC
    LIMIT max_expanded;
END;
$$;

-- ══════════════════════════════════════════════
-- Funcao: get_stats
-- ══════════════════════════════════════════════
CREATE OR REPLACE FUNCTION get_stats()
RETURNS TABLE (
    total_documents BIGINT,
    total_chunks BIGINT
)
LANGUAGE sql
AS $$
    SELECT
        (SELECT COUNT(*) FROM documents) AS total_documents,
        (SELECT COUNT(*) FROM document_chunks) AS total_chunks;
$$;
