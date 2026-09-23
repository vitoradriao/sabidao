\set ON_ERROR_STOP on

BEGIN;

INSERT INTO documents (
    id, filename, title, doc_type, canonical_id, document_revision, schema_version, metadata
) VALUES (
    '60000000-0000-4000-8000-000000000001',
    'canonical-retrieval.md',
    'Documento canônico de teste',
    'md',
    '60000000-0000-4000-8000-000000000002',
    3,
    '1.0.0',
    '{"canonical":{"aliases":["Nome editorial"]}}'::jsonb
);

INSERT INTO document_sections (
    id, document_id, section_index, section_key, heading_path, title, module,
    answer_mode, metadata, retrieval_text, embedding
) VALUES (
    '60000000-0000-4000-8000-000000000003',
    '60000000-0000-4000-8000-000000000001',
    0, 'passo-estavel', 'Documento > Passo', 'Passo', 'teste', 'procedure',
    '{"source_refs":[{"source_id":"manual","locator":{"kind":"line_range","start":7,"end":9}}]}'::jsonb,
    'CANONTEST987 procedimento',
    ('[1,' || repeat('0,', 1534) || '0]')::vector
);

INSERT INTO document_chunks (
    id, document_id, content, retrieval_text, chunk_index, metadata, embedding,
    section_id, module, doc_type, source_type, doc_priority
) VALUES
    (
        '60000000-0000-4000-8000-000000000004',
        '60000000-0000-4000-8000-000000000001',
        'Primeiro passo sem YAML.',
        'CANONTEST987 primeiro passo.',
        0,
        '{"section_key":"passo-estavel","source_refs":[{"source_id":"manual","locator":{"kind":"line_range","start":7,"end":9}}]}'::jsonb,
        ('[1,' || repeat('0,', 1534) || '0]')::vector,
        '60000000-0000-4000-8000-000000000003',
        'teste', 'md', 'fixture', 5
    ),
    (
        '60000000-0000-4000-8000-000000000005',
        '60000000-0000-4000-8000-000000000001',
        'Passo vizinho sem YAML.',
        'Passo vizinho.',
        1,
        '{"section_key":"passo-estavel","source_refs":[{"source_id":"manual","locator":{"kind":"line_range","start":7,"end":9}}]}'::jsonb,
        ('[0,1,' || repeat('0,', 1533) || '0]')::vector,
        '60000000-0000-4000-8000-000000000003',
        'teste', 'md', 'fixture', 5
    );

INSERT INTO documents (id, filename, title, doc_type)
VALUES (
    '60000000-0000-4000-8000-000000000006',
    'legacy-retrieval.md', 'Documento legado de teste', 'md'
);
INSERT INTO document_sections (
    id, document_id, section_index, heading_path, title, module,
    answer_mode, metadata, retrieval_text, embedding
) VALUES (
    '60000000-0000-4000-8000-000000000007',
    '60000000-0000-4000-8000-000000000006',
    0, 'Legado', 'Legado', 'teste', 'procedure', '{}'::jsonb,
    'LEGACYTOKEN987 procedimento',
    ('[0,1,' || repeat('0,', 1533) || '0]')::vector
);
INSERT INTO document_chunks (
    id, document_id, content, retrieval_text, chunk_index, metadata, embedding,
    section_id, module, doc_type, source_type, doc_priority
) VALUES (
    '60000000-0000-4000-8000-000000000008',
    '60000000-0000-4000-8000-000000000006',
    'Passo legado.', 'LEGACYTOKEN987 passo.', 0, '{}'::jsonb,
    ('[0,1,' || repeat('0,', 1533) || '0]')::vector,
    '60000000-0000-4000-8000-000000000007',
    'teste', 'md', 'fixture', 5
);

DO $$
DECLARE
    embedding vector := ('[1,' || repeat('0,', 1534) || '0]')::vector;
    result_row RECORD;
    function_name TEXT;
BEGIN
    FOREACH function_name IN ARRAY ARRAY['match_chunks', 'hybrid_match_chunks'] LOOP
        IF function_name = 'match_chunks' THEN
            SELECT * INTO result_row
            FROM public.match_chunks(embedding, 1, 0.5)
            WHERE filename = 'canonical-retrieval.md' AND is_neighbor = false;
        ELSE
            SELECT * INTO result_row
            FROM public.hybrid_match_chunks(embedding, 'CANONTEST987', 1, 0.5)
            WHERE filename = 'canonical-retrieval.md' AND is_neighbor = false;
        END IF;
        IF NOT FOUND THEN
            RAISE EXCEPTION '% não retornou o chunk canônico', function_name;
        END IF;
        IF result_row.canonical_id IS DISTINCT FROM '60000000-0000-4000-8000-000000000002'::uuid
           OR result_row.document_revision IS DISTINCT FROM 3
           OR result_row.schema_version IS DISTINCT FROM '1.0.0'
           OR result_row.section_key IS DISTINCT FROM 'passo-estavel'
           OR result_row.locator->>'kind' IS DISTINCT FROM 'line_range'
           OR result_row.source_refs->0->>'source_id' IS DISTINCT FROM 'manual'
           OR result_row.aliases->>0 IS DISTINCT FROM 'Nome editorial' THEN
            RAISE EXCEPTION '% não projetou identidade canônica: %', function_name, row_to_json(result_row);
        END IF;
    END LOOP;

    SELECT * INTO result_row
    FROM public.hybrid_match_sections(embedding, 'CANONTEST987', 1, 0.5)
    WHERE filename = 'canonical-retrieval.md';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'hybrid_match_sections não retornou a seção canônica';
    END IF;
    IF result_row.canonical_id IS DISTINCT FROM '60000000-0000-4000-8000-000000000002'::uuid
       OR result_row.document_revision IS DISTINCT FROM 3
       OR result_row.section_key IS DISTINCT FROM 'passo-estavel'
       OR result_row.locator->>'kind' IS DISTINCT FROM 'line_range'
       OR result_row.aliases->>0 IS DISTINCT FROM 'Nome editorial' THEN
        RAISE EXCEPTION 'seção não projetou identidade canônica: %', row_to_json(result_row);
    END IF;

    SELECT * INTO result_row
    FROM public.match_chunks(embedding, 1, 0.5)
    WHERE filename = 'canonical-retrieval.md' AND is_neighbor = true;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'match_chunks não retornou vizinho';
    END IF;
    IF result_row.section_key IS DISTINCT FROM 'passo-estavel'
       OR result_row.locator->>'kind' IS DISTINCT FROM 'line_range' THEN
        RAISE EXCEPTION 'vizinho perdeu projeção canônica';
    END IF;

    SELECT * INTO result_row
    FROM public.hybrid_match_chunks(
        query_embedding => ('[0,1,' || repeat('0,', 1533) || '0]')::vector,
        query_text => 'LEGACYTOKEN987',
        match_count => 2,
        match_threshold => 0.5,
        filter_doc_types => ARRAY['md']::text[]
    )
    WHERE filename = 'legacy-retrieval.md' AND is_neighbor = false;
    IF NOT FOUND OR result_row.canonical_id IS NOT NULL
       OR result_row.section_key IS NOT NULL THEN
        RAISE EXCEPTION 'Busca mista perdeu o documento legado md';
    END IF;

    SELECT * INTO result_row
    FROM public.hybrid_match_sections(
        query_embedding => ('[0,1,' || repeat('0,', 1533) || '0]')::vector,
        query_text => 'LEGACYTOKEN987',
        match_count => 2,
        match_threshold => 0.5,
        filter_doc_types => ARRAY['md']::text[]
    )
    WHERE filename = 'legacy-retrieval.md';
    IF NOT FOUND OR result_row.canonical_id IS NOT NULL THEN
        RAISE EXCEPTION 'Busca de secoes mista perdeu o documento legado md';
    END IF;
END;
$$;

ROLLBACK;
