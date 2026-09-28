-- Projecao canonica nas RPCs de retrieval; sem alteracao de dados ou embeddings.
-- Execute apos migrate_canonical_identity.sql e add_section_retrieval correspondente.
BEGIN;
DROP FUNCTION IF EXISTS public.match_chunks(vector, integer, double precision, text[], text[], uuid[], integer);
DROP FUNCTION IF EXISTS public.match_chunks(vector, integer, real, text[], text[], uuid[], integer);
DROP FUNCTION IF EXISTS public.match_chunks(vector, integer, double precision);
DROP FUNCTION IF EXISTS public.match_chunks(vector, integer, real);
DROP FUNCTION IF EXISTS public.match_chunks(vector, integer, double precision, text[], text[]);
DROP FUNCTION IF EXISTS public.match_chunks(vector, integer, real, text[], text[]);
DROP FUNCTION IF EXISTS public.hybrid_match_chunks(vector, text, integer, double precision, double precision, double precision, text[], text[], uuid[], integer);
DROP FUNCTION IF EXISTS public.hybrid_match_chunks(vector, text, integer, real, real, real, text[], text[], uuid[], integer);
DROP FUNCTION IF EXISTS public.hybrid_match_chunks(vector, text, integer, double precision, double precision, double precision);
DROP FUNCTION IF EXISTS public.hybrid_match_chunks(vector, text, integer, real, real, real);
DROP FUNCTION IF EXISTS public.hybrid_match_chunks(vector, text, integer, double precision, double precision, double precision, text[], text[]);
DROP FUNCTION IF EXISTS public.hybrid_match_chunks(vector, text, integer, real, real, real, text[], text[]);
DROP FUNCTION IF EXISTS public.hybrid_match_sections(vector, text, integer, double precision, double precision, double precision, text[], text[], integer);
DROP FUNCTION IF EXISTS public.hybrid_match_sections(vector, text, integer, real, real, real, text[], text[], integer);

CREATE OR REPLACE FUNCTION public.match_chunks(
    query_embedding VECTOR(1536),
    match_count INT DEFAULT 8,
    match_threshold FLOAT DEFAULT 0.55,
    filter_doc_types TEXT[] DEFAULT NULL,
    filter_modules TEXT[] DEFAULT NULL,
    filter_section_ids UUID[] DEFAULT NULL,
    fetch_limit INT DEFAULT 80
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
    answer_mode TEXT,
    vector_similarity FLOAT,
    lexical_score FLOAT,
    fusion_score FLOAT,
    retrieval_origin TEXT,
    retrieval_rank INTEGER,
    is_neighbor BOOLEAN,
    seed_chunk_id UUID,
    canonical_id UUID,
    document_revision INTEGER,
    schema_version TEXT,
    section_key TEXT,
    locator JSONB,
    source_refs JSONB,
    aliases JSONB
)
LANGUAGE plpgsql
AS $$
DECLARE
    safe_fetch_limit INT := GREATEST(fetch_limit, match_count);
    max_expanded INT := GREATEST(match_count * 3, match_count);
BEGIN
    PERFORM set_config('hnsw.ef_search', GREATEST(100, safe_fetch_limit)::TEXT, true);

    RETURN QUERY
    WITH section_scope AS (
        SELECT DISTINCT neighbor.id AS section_id
        FROM document_sections seed
        JOIN document_sections neighbor
          ON neighbor.document_id = seed.document_id
         AND neighbor.section_index BETWEEN seed.section_index - 1 AND seed.section_index + 1
        WHERE COALESCE(array_length(filter_section_ids, 1), 0) > 0
          AND seed.id = ANY(filter_section_ids)
    ),
    base_chunks AS (
        SELECT
            dc.id,
            dc.document_id,
            dc.content,
            dc.chunk_index,
            dc.metadata,
            dc.embedding,
            d.filename,
            dc.section_id,
            COALESCE(dc.heading_path, ds.heading_path, dc.metadata->>'heading_path', '') AS heading_path,
            COALESCE(dc.semantic_context, ds.semantic_context, dc.metadata->>'semantic_context', '') AS semantic_context,
            COALESCE(NULLIF(dc.entities, '{}'::jsonb), ds.entities, dc.metadata->'entities', '{}'::jsonb) AS entities,
            COALESCE(dc.answer_mode, ds.answer_mode, dc.metadata->>'answer_mode', 'general') AS answer_mode
        FROM document_chunks dc
        JOIN documents d ON d.id = dc.document_id
        LEFT JOIN document_sections ds ON ds.id = dc.section_id
        WHERE (
            COALESCE(array_length(filter_doc_types, 1), 0) = 0
            OR COALESCE(dc.doc_type, d.doc_type, dc.metadata->>'doc_type', '') = ANY(filter_doc_types)
        )
        AND (
            COALESCE(array_length(filter_modules, 1), 0) = 0
            OR COALESCE(dc.module, ds.module, dc.metadata->>'module', '') = ANY(filter_modules)
            OR (
                COALESCE(dc.module, ds.module, dc.metadata->>'module', '') = ''
                AND NOT (dc.metadata ? 'module')
            )
        )
        AND (
            COALESCE(array_length(filter_section_ids, 1), 0) = 0
            OR dc.section_id IN (SELECT ss.section_id FROM section_scope ss)
        )
    ),
    vector_candidates AS (
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
        ORDER BY bc.embedding <=> query_embedding ASC, bc.id ASC
        LIMIT safe_fetch_limit
    ),
    top_ranked AS (
        SELECT
            vc.*,
            ROW_NUMBER() OVER (
                ORDER BY vc.similarity DESC, vc.id ASC
            )::INTEGER AS retrieval_rank
        FROM vector_candidates vc
        ORDER BY vc.similarity DESC, vc.id ASC
        LIMIT match_count
    ),
    expanded_candidates AS (
        SELECT
            bc.id,
            bc.document_id,
            bc.content,
            bc.chunk_index,
            bc.metadata,
            bc.filename,
            (1 - (bc.embedding <=> query_embedding))::FLOAT AS vector_similarity,
            bc.section_id,
            bc.heading_path,
            bc.semantic_context,
            bc.entities,
            bc.answer_mode,
            tm.retrieval_rank,
            (bc.id <> tm.id) AS is_neighbor,
            tm.id AS seed_chunk_id,
            ABS(bc.chunk_index - tm.chunk_index) AS neighbor_distance,
            ROW_NUMBER() OVER (
                PARTITION BY bc.id
                ORDER BY
                    (bc.id <> tm.id) ASC,
                    tm.retrieval_rank ASC,
                    ABS(bc.chunk_index - tm.chunk_index) ASC,
                    tm.id ASC
            ) AS expansion_choice
        FROM top_ranked tm
        JOIN base_chunks bc
          ON bc.document_id = tm.document_id
         AND bc.chunk_index BETWEEN tm.chunk_index - 1 AND tm.chunk_index + 1
    )
    SELECT
        ec.id,
        ec.document_id,
        ec.content,
        ec.chunk_index,
        ec.metadata,
        ec.filename,
        ec.vector_similarity AS similarity,
        ec.section_id,
        ec.heading_path,
        ec.semantic_context,
        ec.entities,
        ec.answer_mode,
        ec.vector_similarity,
        NULL::FLOAT AS lexical_score,
        NULL::FLOAT AS fusion_score,
        CASE WHEN ec.is_neighbor THEN 'neighbor' ELSE 'vector' END AS retrieval_origin,
        ec.retrieval_rank,
        ec.is_neighbor,
        ec.seed_chunk_id,
        d.canonical_id,
        d.document_revision,
        d.schema_version,
        COALESCE(ds.section_key, ec.metadata->>'section_key') AS section_key,
        COALESCE(ec.metadata->'source_refs', ds.metadata->'source_refs') -> 0 -> 'locator' AS locator,
        COALESCE(ec.metadata->'source_refs', ds.metadata->'source_refs') AS source_refs,
        d.metadata->'canonical'->'aliases' AS aliases
    FROM expanded_candidates ec
    JOIN documents d ON d.id = ec.document_id
    LEFT JOIN document_sections ds ON ds.id = ec.section_id
    WHERE ec.expansion_choice = 1
    ORDER BY
        ec.retrieval_rank ASC,
        ec.is_neighbor ASC,
        ec.neighbor_distance ASC,
        ec.chunk_index ASC,
        ec.id ASC
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
    filter_modules TEXT[] DEFAULT NULL,
    filter_section_ids UUID[] DEFAULT NULL,
    fetch_limit INT DEFAULT 80
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
    answer_mode TEXT,
    vector_similarity FLOAT,
    lexical_score FLOAT,
    fusion_score FLOAT,
    retrieval_origin TEXT,
    retrieval_rank INTEGER,
    is_neighbor BOOLEAN,
    seed_chunk_id UUID,
    canonical_id UUID,
    document_revision INTEGER,
    schema_version TEXT,
    section_key TEXT,
    locator JSONB,
    source_refs JSONB,
    aliases JSONB
)
LANGUAGE plpgsql
AS $$
DECLARE
    rrf_k CONSTANT INT := 60;
    safe_fetch_limit INT := GREATEST(fetch_limit, match_count);
    max_expanded INT := GREATEST(match_count * 3, match_count);
BEGIN
    PERFORM set_config('hnsw.ef_search', GREATEST(100, safe_fetch_limit)::TEXT, true);

    RETURN QUERY
    WITH section_scope AS (
        SELECT DISTINCT neighbor.id AS section_id
        FROM document_sections seed
        JOIN document_sections neighbor
          ON neighbor.document_id = seed.document_id
         AND neighbor.section_index BETWEEN seed.section_index - 1 AND seed.section_index + 1
        WHERE COALESCE(array_length(filter_section_ids, 1), 0) > 0
          AND seed.id = ANY(filter_section_ids)
    ),
    base_chunks AS (
        SELECT
            dc.id,
            dc.document_id,
            dc.content,
            dc.chunk_index,
            dc.metadata,
            dc.embedding,
            dc.retrieval_fts AS fts,
            d.filename,
            dc.section_id,
            COALESCE(dc.heading_path, ds.heading_path, dc.metadata->>'heading_path', '') AS heading_path,
            COALESCE(dc.semantic_context, ds.semantic_context, dc.metadata->>'semantic_context', '') AS semantic_context,
            COALESCE(NULLIF(dc.entities, '{}'::jsonb), ds.entities, dc.metadata->'entities', '{}'::jsonb) AS entities,
            COALESCE(dc.answer_mode, ds.answer_mode, dc.metadata->>'answer_mode', 'general') AS answer_mode
        FROM document_chunks dc
        JOIN documents d ON d.id = dc.document_id
        LEFT JOIN document_sections ds ON ds.id = dc.section_id
        WHERE (
            COALESCE(array_length(filter_doc_types, 1), 0) = 0
            OR COALESCE(dc.doc_type, d.doc_type, dc.metadata->>'doc_type', '') = ANY(filter_doc_types)
        )
        AND (
            COALESCE(array_length(filter_modules, 1), 0) = 0
            OR COALESCE(dc.module, ds.module, dc.metadata->>'module', '') = ANY(filter_modules)
            OR (
                COALESCE(dc.module, ds.module, dc.metadata->>'module', '') = ''
                AND NOT (dc.metadata ? 'module')
            )
        )
        AND (
            COALESCE(array_length(filter_section_ids, 1), 0) = 0
            OR dc.section_id IN (SELECT ss.section_id FROM section_scope ss)
        )
    ),
    vector_results AS (
        SELECT
            bc.id,
            ROW_NUMBER() OVER (
                ORDER BY bc.embedding <=> query_embedding ASC, bc.id ASC
            ) AS rank_pos,
            (1 - (bc.embedding <=> query_embedding))::FLOAT AS vec_similarity
        FROM base_chunks bc
        WHERE 1 - (bc.embedding <=> query_embedding) >= match_threshold * 0.7
        ORDER BY bc.embedding <=> query_embedding ASC, bc.id ASC
        LIMIT safe_fetch_limit
    ),
    fts_results AS (
        SELECT
            bc.id,
            ROW_NUMBER() OVER (
                ORDER BY
                    ts_rank_cd(
                        bc.fts,
                        websearch_to_tsquery('portuguese', NULLIF(BTRIM(query_text), ''))
                    ) DESC,
                    bc.id ASC
            ) AS rank_pos,
            ts_rank_cd(
                bc.fts,
                websearch_to_tsquery('portuguese', NULLIF(BTRIM(query_text), ''))
            )::FLOAT AS fts_rank
        FROM base_chunks bc
        WHERE bc.fts @@ websearch_to_tsquery(
            'portuguese',
            NULLIF(BTRIM(query_text), '')
        )
        ORDER BY ts_rank_cd(
            bc.fts,
            websearch_to_tsquery('portuguese', NULLIF(BTRIM(query_text), ''))
        ) DESC, bc.id ASC
        LIMIT safe_fetch_limit
    ),
    combined AS (
        SELECT
            COALESCE(vr.id, fr.id) AS chunk_id,
            COALESCE(vector_weight * (1.0 / (rrf_k + vr.rank_pos)), 0) +
            COALESCE(fts_weight * (1.0 / (rrf_k + fr.rank_pos)), 0) AS rrf_score,
            vr.vec_similarity::FLOAT AS vec_similarity,
            fr.fts_rank::FLOAT AS fts_rank,
            CASE
                WHEN vr.id IS NOT NULL AND fr.id IS NOT NULL THEN 'hybrid'
                WHEN fr.id IS NOT NULL THEN 'lexical'
                ELSE 'vector'
            END AS retrieval_origin
        FROM vector_results vr
        FULL OUTER JOIN fts_results fr ON vr.id = fr.id
    ),
    ranked_matches AS (
        SELECT
            c.chunk_id,
            c.rrf_score,
            c.vec_similarity,
            c.fts_rank,
            c.retrieval_origin,
            ROW_NUMBER() OVER (
                ORDER BY
                    c.rrf_score DESC,
                    COALESCE(c.vec_similarity, -1.0) DESC,
                    c.chunk_id ASC
            )::INTEGER AS retrieval_rank
        FROM combined c
    ),
    top_matches AS (
        SELECT rm.*
        FROM ranked_matches rm
        ORDER BY rm.retrieval_rank
        LIMIT match_count
    ),
    expanded_candidates AS (
        SELECT
            bc.id,
            bc.document_id,
            bc.content,
            bc.chunk_index,
            bc.metadata,
            bc.filename,
            (1 - (bc.embedding <=> query_embedding))::FLOAT AS vector_similarity,
            bc.section_id,
            bc.heading_path,
            bc.semantic_context,
            bc.entities,
            bc.answer_mode,
            CASE WHEN bc.id = tm.chunk_id THEN tm.fts_rank ELSE NULL END AS lexical_score,
            CASE WHEN bc.id = tm.chunk_id THEN tm.rrf_score ELSE NULL END AS fusion_score,
            CASE
                WHEN bc.id = tm.chunk_id THEN tm.retrieval_origin
                ELSE 'neighbor'
            END AS retrieval_origin,
            tm.retrieval_rank,
            (bc.id <> tm.chunk_id) AS is_neighbor,
            tm.chunk_id AS seed_chunk_id,
            ABS(bc.chunk_index - seed.chunk_index) AS neighbor_distance,
            ROW_NUMBER() OVER (
                PARTITION BY bc.id
                ORDER BY
                    (bc.id <> tm.chunk_id) ASC,
                    tm.retrieval_rank ASC,
                    ABS(bc.chunk_index - seed.chunk_index) ASC,
                    tm.chunk_id ASC
            ) AS expansion_choice
        FROM top_matches tm
        JOIN base_chunks seed ON seed.id = tm.chunk_id
        JOIN base_chunks bc
          ON bc.document_id = seed.document_id
         AND bc.chunk_index BETWEEN seed.chunk_index - 1 AND seed.chunk_index + 1
    )
    SELECT
        ec.id,
        ec.document_id,
        ec.content,
        ec.chunk_index,
        ec.metadata,
        ec.filename,
        ec.vector_similarity AS similarity,
        ec.section_id,
        ec.heading_path,
        ec.semantic_context,
        ec.entities,
        ec.answer_mode,
        ec.vector_similarity,
        ec.lexical_score,
        ec.fusion_score,
        ec.retrieval_origin,
        ec.retrieval_rank,
        ec.is_neighbor,
        ec.seed_chunk_id,
        d.canonical_id,
        d.document_revision,
        d.schema_version,
        COALESCE(ds.section_key, ec.metadata->>'section_key') AS section_key,
        COALESCE(ec.metadata->'source_refs', ds.metadata->'source_refs') -> 0 -> 'locator' AS locator,
        COALESCE(ec.metadata->'source_refs', ds.metadata->'source_refs') AS source_refs,
        d.metadata->'canonical'->'aliases' AS aliases
    FROM expanded_candidates ec
    JOIN documents d ON d.id = ec.document_id
    LEFT JOIN document_sections ds ON ds.id = ec.section_id
    WHERE ec.expansion_choice = 1
    ORDER BY
        ec.retrieval_rank ASC,
        ec.is_neighbor ASC,
        ec.neighbor_distance ASC,
        ec.chunk_index ASC,
        ec.id ASC
    LIMIT max_expanded;
END;
$$;

CREATE OR REPLACE FUNCTION public.hybrid_match_sections(
    query_embedding VECTOR(1536),
    query_text TEXT,
    match_count INT DEFAULT 12,
    match_threshold FLOAT DEFAULT 0.55,
    vector_weight FLOAT DEFAULT 0.65,
    fts_weight FLOAT DEFAULT 0.35,
    filter_doc_types TEXT[] DEFAULT NULL,
    filter_modules TEXT[] DEFAULT NULL,
    fetch_limit INT DEFAULT 48
)
RETURNS TABLE (
    id UUID,
    document_id UUID,
    section_index INTEGER,
    heading_path TEXT,
    title TEXT,
    module TEXT,
    answer_mode TEXT,
    semantic_context TEXT,
    entities JSONB,
    retrieval_text TEXT,
    filename TEXT,
    similarity FLOAT,
    vector_similarity FLOAT,
    lexical_score FLOAT,
    fusion_score FLOAT,
    retrieval_origin TEXT,
    retrieval_rank INTEGER,
    canonical_id UUID,
    document_revision INTEGER,
    schema_version TEXT,
    section_key TEXT,
    locator JSONB,
    source_refs JSONB,
    aliases JSONB
)
LANGUAGE plpgsql
AS $$
DECLARE
    rrf_k CONSTANT INT := 60;
    safe_fetch_limit INT := GREATEST(fetch_limit, match_count);
BEGIN
    PERFORM set_config('hnsw.ef_search', GREATEST(100, safe_fetch_limit)::TEXT, true);

    RETURN QUERY
    WITH base_sections AS (
        SELECT
            ds.id,
            ds.document_id,
            ds.section_index,
            ds.heading_path,
            ds.title,
            COALESCE(ds.module, '') AS module,
            ds.answer_mode,
            ds.semantic_context,
            ds.entities,
            ds.retrieval_text,
            ds.embedding,
            ds.fts,
            d.filename,
            COALESCE(d.doc_type, '') AS doc_type
        FROM document_sections ds
        JOIN documents d ON d.id = ds.document_id
        WHERE (
            COALESCE(array_length(filter_doc_types, 1), 0) = 0
            OR COALESCE(d.doc_type, '') = ANY(filter_doc_types)
        )
        AND (
            COALESCE(array_length(filter_modules, 1), 0) = 0
            OR COALESCE(ds.module, '') = ANY(filter_modules)
            OR COALESCE(ds.module, '') = ''
        )
    ),
    vector_results AS (
        SELECT
            bs.id,
            ROW_NUMBER() OVER (
                ORDER BY bs.embedding <=> query_embedding ASC, bs.id ASC
            ) AS rank_pos,
            (1 - (bs.embedding <=> query_embedding))::FLOAT AS vec_similarity
        FROM base_sections bs
        WHERE bs.embedding IS NOT NULL
          AND 1 - (bs.embedding <=> query_embedding) >= match_threshold * 0.65
        ORDER BY bs.embedding <=> query_embedding ASC, bs.id ASC
        LIMIT safe_fetch_limit
    ),
    fts_results AS (
        SELECT
            bs.id,
            ROW_NUMBER() OVER (
                ORDER BY
                    ts_rank_cd(
                        bs.fts,
                        websearch_to_tsquery('portuguese', NULLIF(BTRIM(query_text), ''))
                    ) DESC,
                    bs.id ASC
            ) AS rank_pos,
            ts_rank_cd(
                bs.fts,
                websearch_to_tsquery('portuguese', NULLIF(BTRIM(query_text), ''))
            )::FLOAT AS fts_rank
        FROM base_sections bs
        WHERE bs.fts @@ websearch_to_tsquery(
            'portuguese',
            NULLIF(BTRIM(query_text), '')
        )
        ORDER BY ts_rank_cd(
            bs.fts,
            websearch_to_tsquery('portuguese', NULLIF(BTRIM(query_text), ''))
        ) DESC, bs.id ASC
        LIMIT safe_fetch_limit
    ),
    combined AS (
        SELECT
            COALESCE(vr.id, fr.id) AS section_id,
            COALESCE(vector_weight * (1.0 / (rrf_k + vr.rank_pos)), 0) +
            COALESCE(fts_weight * (1.0 / (rrf_k + fr.rank_pos)), 0) AS rrf_score,
            vr.vec_similarity::FLOAT AS vec_similarity,
            fr.fts_rank::FLOAT AS fts_rank,
            CASE
                WHEN vr.id IS NOT NULL AND fr.id IS NOT NULL THEN 'hybrid'
                WHEN fr.id IS NOT NULL THEN 'lexical'
                ELSE 'vector'
            END AS retrieval_origin
        FROM vector_results vr
        FULL OUTER JOIN fts_results fr ON vr.id = fr.id
    ),
    ranked_matches AS (
        SELECT
            c.section_id,
            c.rrf_score,
            c.vec_similarity,
            c.fts_rank,
            c.retrieval_origin,
            ROW_NUMBER() OVER (
                ORDER BY
                    c.rrf_score DESC,
                    COALESCE(c.vec_similarity, -1.0) DESC,
                    c.section_id ASC
            )::INTEGER AS retrieval_rank
        FROM combined c
    )
    SELECT
        bs.id,
        bs.document_id,
        bs.section_index,
        bs.heading_path,
        bs.title,
        bs.module,
        bs.answer_mode,
        bs.semantic_context,
        bs.entities,
        bs.retrieval_text,
        bs.filename,
        COALESCE(
            (1 - (bs.embedding <=> query_embedding))::FLOAT,
            0.0
        ) AS similarity,
        COALESCE(
            (1 - (bs.embedding <=> query_embedding))::FLOAT,
            0.0
        ) AS vector_similarity,
        rm.fts_rank AS lexical_score,
        rm.rrf_score AS fusion_score,
        rm.retrieval_origin,
        rm.retrieval_rank,
        d.canonical_id,
        d.document_revision,
        d.schema_version,
        ds.section_key,
        ds.metadata->'source_refs'->0->'locator' AS locator,
        ds.metadata->'source_refs' AS source_refs,
        d.metadata->'canonical'->'aliases' AS aliases
    FROM ranked_matches rm
    JOIN base_sections bs ON bs.id = rm.section_id
    JOIN document_sections ds ON ds.id = bs.id
    JOIN documents d ON d.id = bs.document_id
    ORDER BY rm.retrieval_rank
    LIMIT match_count;
END;
$$;

NOTIFY pgrst, 'reload schema';
COMMIT;
