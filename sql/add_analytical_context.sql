-- Analytical context layer for document sections and enriched chunks.
-- Additive migration: keeps existing documents/document_chunks data.

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS document_sections (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    document_id UUID NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    section_index INTEGER NOT NULL,
    section_key TEXT NULL,
    heading_path TEXT NOT NULL DEFAULT '',
    title TEXT,
    module TEXT,
    answer_mode TEXT NOT NULL DEFAULT 'general',
    semantic_context TEXT,
    entities JSONB NOT NULL DEFAULT '{}'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS document_sections_doc_idx_unique
ON document_sections(document_id, section_index);

CREATE UNIQUE INDEX IF NOT EXISTS document_sections_document_section_key_unique
ON document_sections(document_id, section_key)
WHERE section_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS document_sections_document_id_idx
ON document_sections(document_id);

CREATE INDEX IF NOT EXISTS document_sections_answer_mode_idx
ON document_sections(answer_mode);

CREATE INDEX IF NOT EXISTS document_sections_entities_gin_idx
ON document_sections USING gin(entities);

ALTER TABLE document_chunks
ADD COLUMN IF NOT EXISTS section_id UUID REFERENCES document_sections(id) ON DELETE SET NULL;

ALTER TABLE document_chunks
ADD COLUMN IF NOT EXISTS heading_path TEXT;

ALTER TABLE document_chunks
ADD COLUMN IF NOT EXISTS semantic_context TEXT;

ALTER TABLE document_chunks
ADD COLUMN IF NOT EXISTS entities JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE document_chunks
ADD COLUMN IF NOT EXISTS answer_mode TEXT;

CREATE INDEX IF NOT EXISTS document_chunks_section_id_idx
ON document_chunks(section_id);

CREATE INDEX IF NOT EXISTS document_chunks_answer_mode_idx
ON document_chunks(answer_mode);

CREATE INDEX IF NOT EXISTS document_chunks_entities_gin_idx
ON document_chunks USING gin(entities);

DROP FUNCTION IF EXISTS public.match_chunks(vector, integer, double precision);
DROP FUNCTION IF EXISTS public.match_chunks(vector, integer, real);
DROP FUNCTION IF EXISTS public.match_chunks(vector, integer, double precision, text[], text[]);
DROP FUNCTION IF EXISTS public.match_chunks(vector, integer, real, text[], text[]);
DROP FUNCTION IF EXISTS public.hybrid_match_chunks(vector, text, integer, double precision, double precision, double precision);
DROP FUNCTION IF EXISTS public.hybrid_match_chunks(vector, text, integer, real, real, real);
DROP FUNCTION IF EXISTS public.hybrid_match_chunks(vector, text, integer, double precision, double precision, double precision, text[], text[]);
DROP FUNCTION IF EXISTS public.hybrid_match_chunks(vector, text, integer, real, real, real, text[], text[]);

CREATE OR REPLACE FUNCTION public.match_chunks(
    query_embedding VECTOR(1536),
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
    similarity FLOAT,
    section_id UUID,
    heading_path TEXT,
    semantic_context TEXT,
    entities JSONB,
    answer_mode TEXT
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
            d.doc_type,
            dc.section_id,
            COALESCE(dc.heading_path, dc.metadata->>'heading_path', '') AS heading_path,
            COALESCE(dc.semantic_context, dc.metadata->>'semantic_context', '') AS semantic_context,
            COALESCE(NULLIF(dc.entities, '{}'::jsonb), dc.metadata->'entities', '{}'::jsonb) AS entities,
            COALESCE(dc.answer_mode, dc.metadata->>'answer_mode', 'general') AS answer_mode
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
            bc.id,
            bc.document_id,
            bc.content,
            bc.chunk_index,
            bc.metadata,
            bc.filename,
            COALESCE(
                tm.similarity,
                (
                    SELECT MAX(tm2.similarity) * 0.90
                    FROM top_matches tm2
                    WHERE tm2.document_id = bc.document_id
                      AND ABS(tm2.chunk_index - bc.chunk_index) = 1
                )
            )::FLOAT AS similarity,
            bc.section_id,
            bc.heading_path,
            bc.semantic_context,
            bc.entities,
            bc.answer_mode
        FROM neighbor_indices ni
        JOIN base_chunks bc ON bc.document_id = ni.doc_id AND bc.chunk_index = ni.idx
        LEFT JOIN top_matches tm ON tm.id = bc.id
    )
    SELECT
        f.id,
        f.document_id,
        f.content,
        f.chunk_index,
        f.metadata,
        f.filename,
        f.similarity,
        f.section_id,
        f.heading_path,
        f.semantic_context,
        f.entities,
        f.answer_mode
    FROM (
        SELECT DISTINCT ON (e.id)
            e.id,
            e.document_id,
            e.content,
            e.chunk_index,
            e.metadata,
            e.filename,
            e.similarity,
            e.section_id,
            e.heading_path,
            e.semantic_context,
            e.entities,
            e.answer_mode
        FROM expanded e
        WHERE e.similarity IS NOT NULL
        ORDER BY e.id, e.similarity DESC
    ) f
    ORDER BY f.similarity DESC
    LIMIT max_expanded;
END;
$$;

CREATE OR REPLACE FUNCTION public.hybrid_match_chunks(
    query_embedding VECTOR(1536),
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
    similarity FLOAT,
    section_id UUID,
    heading_path TEXT,
    semantic_context TEXT,
    entities JSONB,
    answer_mode TEXT
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
            d.doc_type,
            dc.section_id,
            COALESCE(dc.heading_path, dc.metadata->>'heading_path', '') AS heading_path,
            COALESCE(dc.semantic_context, dc.metadata->>'semantic_context', '') AS semantic_context,
            COALESCE(NULLIF(dc.entities, '{}'::jsonb), dc.metadata->'entities', '{}'::jsonb) AS entities,
            COALESCE(dc.answer_mode, dc.metadata->>'answer_mode', 'general') AS answer_mode
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
            bc.id,
            bc.document_id,
            bc.content,
            bc.chunk_index,
            bc.metadata,
            bc.filename,
            COALESCE(
                tm.vec_similarity,
                (
                    SELECT MAX(tm2.vec_similarity) * 0.90
                    FROM top_matches tm2
                    JOIN base_chunks bc2 ON bc2.id = tm2.chunk_id
                    WHERE bc2.document_id = bc.document_id
                      AND ABS(bc2.chunk_index - bc.chunk_index) = 1
                )
            )::FLOAT AS similarity,
            bc.section_id,
            bc.heading_path,
            bc.semantic_context,
            bc.entities,
            bc.answer_mode
        FROM neighbor_indices ni
        JOIN base_chunks bc ON bc.document_id = ni.doc_id AND bc.chunk_index = ni.idx
        LEFT JOIN top_matches tm ON tm.chunk_id = bc.id
    )
    SELECT
        f.id,
        f.document_id,
        f.content,
        f.chunk_index,
        f.metadata,
        f.filename,
        f.similarity,
        f.section_id,
        f.heading_path,
        f.semantic_context,
        f.entities,
        f.answer_mode
    FROM (
        SELECT DISTINCT ON (e.id)
            e.id,
            e.document_id,
            e.content,
            e.chunk_index,
            e.metadata,
            e.filename,
            e.similarity,
            e.section_id,
            e.heading_path,
            e.semantic_context,
            e.entities,
            e.answer_mode
        FROM expanded e
        WHERE e.similarity IS NOT NULL
        ORDER BY e.id, e.similarity DESC
    ) f
    ORDER BY f.similarity DESC
    LIMIT max_expanded;
END;
$$;

NOTIFY pgrst, 'reload schema';
