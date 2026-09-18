\set ON_ERROR_STOP on

BEGIN;

INSERT INTO documents (id, filename, title, doc_type)
VALUES
    ('00000000-0000-0000-0000-000000000100', 'lexical.md', 'Lexical', 'md'),
    ('00000000-0000-0000-0000-000000000200', 'vector.md', 'Vector', 'md');

INSERT INTO document_sections (
    id,
    document_id,
    section_index,
    heading_path,
    title,
    module,
    answer_mode,
    retrieval_text,
    embedding
)
VALUES
    (
        '00000000-0000-0000-0000-000000000110',
        '00000000-0000-0000-0000-000000000100',
        0,
        'Lexical',
        'Lexical',
        'teste',
        'general',
        'Procedimento para CODIGOXYZ987',
        ('[0,1,' || repeat('0,', 1533) || '0]')::vector
    ),
    (
        '00000000-0000-0000-0000-000000000210',
        '00000000-0000-0000-0000-000000000200',
        0,
        'Vector',
        'Vector',
        'teste',
        'general',
        'Procedimento generico',
        ('[1,' || repeat('0,', 1534) || '0]')::vector
    );

INSERT INTO document_chunks (
    id,
    document_id,
    content,
    chunk_index,
    metadata,
    embedding,
    section_id,
    module,
    doc_type,
    source_type,
    doc_priority
)
VALUES
    (
        '00000000-0000-0000-0000-000000000101',
        '00000000-0000-0000-0000-000000000100',
        'Contexto anterior do identificador.',
        0,
        '{}'::jsonb,
        ('[0,1,' || repeat('0,', 1533) || '0]')::vector,
        '00000000-0000-0000-0000-000000000110',
        'teste',
        'md',
        'fixture',
        5
    ),
    (
        '00000000-0000-0000-0000-000000000102',
        '00000000-0000-0000-0000-000000000100',
        'Executar o procedimento do identificador CODIGOXYZ987.',
        1,
        '{}'::jsonb,
        ('[0,1,' || repeat('0,', 1533) || '0]')::vector,
        '00000000-0000-0000-0000-000000000110',
        'teste',
        'md',
        'fixture',
        5
    ),
    (
        '00000000-0000-0000-0000-000000000201',
        '00000000-0000-0000-0000-000000000200',
        'Conteudo semanticamente proximo, sem o identificador exato.',
        0,
        '{}'::jsonb,
        ('[1,' || repeat('0,', 1534) || '0]')::vector,
        '00000000-0000-0000-0000-000000000210',
        'teste',
        'md',
        'fixture',
        5
    ),
    (
        '00000000-0000-0000-0000-000000000202',
        '00000000-0000-0000-0000-000000000200',
        'Contexto vetorial adjacente com baixa similaridade própria.',
        1,
        '{}'::jsonb,
        ('[0,1,' || repeat('0,', 1533) || '0]')::vector,
        '00000000-0000-0000-0000-000000000210',
        'teste',
        'md',
        'fixture',
        5
    );

DO $$
DECLARE
    query_vector vector := ('[1,' || repeat('0,', 1534) || '0]')::vector;
    ordered_ids UUID[];
    lexical_origin TEXT;
    lexical_fusion FLOAT;
    neighbor_origin TEXT;
    neighbor_fusion FLOAT;
    neighbor_similarity FLOAT;
    neighbor_seed UUID;
    section_ids UUID[];
    vector_ids UUID[];
    repeated_vector_ids UUID[];
    filtered_count INTEGER;
    pure_vector_ids UUID[];
    pure_neighbor_origin TEXT;
    pure_neighbor_similarity FLOAT;
    pure_neighbor_fusion FLOAT;
BEGIN
    SELECT array_agg(hit.id ORDER BY hit.ordinality)
    INTO ordered_ids
    FROM public.hybrid_match_chunks(
        query_vector,
        'CODIGOXYZ987',
        3,
        0.5,
        0.1,
        0.9,
        ARRAY['md']::TEXT[],
        ARRAY['teste']::TEXT[],
        NULL,
        12
    ) WITH ORDINALITY AS hit;

    IF ordered_ids[1] <> '00000000-0000-0000-0000-000000000102'::UUID THEN
        RAISE EXCEPTION 'RRF order was not preserved: %', ordered_ids;
    END IF;
    IF NOT ('00000000-0000-0000-0000-000000000201'::UUID = ANY(ordered_ids)) THEN
        RAISE EXCEPTION 'Vector candidate missing: %', ordered_ids;
    END IF;

    SELECT hit.retrieval_origin, hit.fusion_score
    INTO lexical_origin, lexical_fusion
    FROM public.hybrid_match_chunks(
        query_vector,
        'CODIGOXYZ987',
        3,
        0.5,
        0.1,
        0.9,
        ARRAY['md']::TEXT[],
        ARRAY['teste']::TEXT[],
        NULL,
        12
    ) AS hit
    WHERE hit.id = '00000000-0000-0000-0000-000000000102'::UUID;

    IF lexical_origin <> 'lexical' OR lexical_fusion IS NULL THEN
        RAISE EXCEPTION 'Lexical-only candidate lost its origin/score: %, %',
            lexical_origin,
            lexical_fusion;
    END IF;

    SELECT
        hit.retrieval_origin,
        hit.fusion_score,
        hit.similarity,
        hit.seed_chunk_id
    INTO neighbor_origin, neighbor_fusion, neighbor_similarity, neighbor_seed
    FROM public.hybrid_match_chunks(
        query_vector,
        'CODIGOXYZ987',
        3,
        0.5,
        0.1,
        0.9,
        ARRAY['md']::TEXT[],
        ARRAY['teste']::TEXT[],
        NULL,
        12
    ) AS hit
    WHERE hit.id = '00000000-0000-0000-0000-000000000101'::UUID;

    IF neighbor_origin <> 'neighbor'
       OR neighbor_fusion IS NOT NULL
       OR neighbor_similarity <> 0.0
       OR neighbor_seed <> '00000000-0000-0000-0000-000000000102'::UUID THEN
        RAISE EXCEPTION 'Neighbor metadata is invalid: %, %, %, %',
            neighbor_origin,
            neighbor_fusion,
            neighbor_similarity,
            neighbor_seed;
    END IF;

    SELECT array_agg(hit.id ORDER BY hit.ordinality)
    INTO section_ids
    FROM public.hybrid_match_sections(
        query_vector,
        'CODIGOXYZ987',
        2,
        0.5,
        0.1,
        0.9,
        ARRAY['md']::TEXT[],
        ARRAY['teste']::TEXT[],
        12
    ) WITH ORDINALITY AS hit;

    IF section_ids[1] <> '00000000-0000-0000-0000-000000000110'::UUID THEN
        RAISE EXCEPTION 'Section RRF order was not preserved: %', section_ids;
    END IF;

    SELECT array_agg(hit.id ORDER BY hit.ordinality)
    INTO vector_ids
    FROM public.hybrid_match_chunks(
        query_vector,
        '',
        2,
        0.5,
        0.6,
        0.4,
        ARRAY['md']::TEXT[],
        ARRAY['teste']::TEXT[],
        NULL,
        12
    ) WITH ORDINALITY AS hit;

    SELECT array_agg(hit.id ORDER BY hit.ordinality)
    INTO repeated_vector_ids
    FROM public.hybrid_match_chunks(
        query_vector,
        '',
        2,
        0.5,
        0.6,
        0.4,
        ARRAY['md']::TEXT[],
        ARRAY['teste']::TEXT[],
        NULL,
        12
    ) WITH ORDINALITY AS hit;

    IF vector_ids[1] <> '00000000-0000-0000-0000-000000000201'::UUID
       OR vector_ids IS DISTINCT FROM repeated_vector_ids THEN
        RAISE EXCEPTION 'Vector/empty query is not deterministic: % <> %',
            vector_ids,
            repeated_vector_ids;
    END IF;

    SELECT COUNT(*)
    INTO filtered_count
    FROM public.hybrid_match_chunks(
        query_vector,
        'CODIGOXYZ987',
        3,
        0.5,
        0.1,
        0.9,
        ARRAY['md']::TEXT[],
        ARRAY['outro_modulo']::TEXT[],
        NULL,
        12
    );

    IF filtered_count <> 0 THEN
        RAISE EXCEPTION 'Module filter returned % unexpected rows', filtered_count;
    END IF;

    SELECT array_agg(hit.id ORDER BY hit.ordinality)
    INTO pure_vector_ids
    FROM public.match_chunks(
        query_vector,
        1,
        0.5,
        ARRAY['md']::TEXT[],
        ARRAY['teste']::TEXT[],
        NULL,
        12
    ) WITH ORDINALITY AS hit;

    IF pure_vector_ids[1] <> '00000000-0000-0000-0000-000000000201'::UUID
       OR NOT ('00000000-0000-0000-0000-000000000202'::UUID = ANY(pure_vector_ids)) THEN
        RAISE EXCEPTION 'Pure vector expansion is invalid: %', pure_vector_ids;
    END IF;

    SELECT hit.retrieval_origin, hit.similarity, hit.fusion_score
    INTO pure_neighbor_origin, pure_neighbor_similarity, pure_neighbor_fusion
    FROM public.match_chunks(
        query_vector,
        1,
        0.5,
        ARRAY['md']::TEXT[],
        ARRAY['teste']::TEXT[],
        NULL,
        12
    ) AS hit
    WHERE hit.id = '00000000-0000-0000-0000-000000000202'::UUID;

    IF pure_neighbor_origin <> 'neighbor'
       OR pure_neighbor_similarity <> 0.0
       OR pure_neighbor_fusion IS NOT NULL THEN
        RAISE EXCEPTION 'Pure vector neighbor metadata is invalid: %, %, %',
            pure_neighbor_origin,
            pure_neighbor_similarity,
            pure_neighbor_fusion;
    END IF;
END;
$$;

ROLLBACK;
