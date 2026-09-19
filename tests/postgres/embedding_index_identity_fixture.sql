BEGIN;

TRUNCATE TABLE
    public.embedding_index_identities,
    public.document_chunks,
    public.document_sections,
    public.feedback_chunks
CASCADE;

SELECT * FROM public.ensure_embedding_index_identity(
    'corpus',
    'gemini',
    'gemini-embedding-001',
    1536,
    'rag-text-v1'
);

SELECT * FROM public.ensure_embedding_index_identity(
    'sections',
    'gemini',
    'gemini-embedding-001',
    1536,
    'rag-text-v1'
);

SELECT * FROM public.ensure_embedding_index_identity(
    'feedback',
    'gemini',
    'gemini-embedding-001',
    1536,
    'rag-text-v1'
);

DO $$
DECLARE
    identity_count INTEGER;
BEGIN
    SELECT COUNT(*)
    INTO identity_count
    FROM public.embedding_index_identities;

    IF identity_count <> 3 THEN
        RAISE EXCEPTION 'Esperadas 3 identidades vetoriais, obtidas %.', identity_count;
    END IF;
END;
$$;

DO $$
BEGIN
    PERFORM public.ensure_embedding_index_identity(
        'corpus',
        'gemini',
        'outro-modelo-com-1536-dimensoes',
        1536,
        'rag-text-v1'
    );
    RAISE EXCEPTION 'Troca de modelo com a mesma dimensao deveria falhar.';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLERRM NOT LIKE 'Identidade vetorial incompativel no escopo corpus:%' THEN
            RAISE;
        END IF;
END;
$$;

DELETE FROM public.embedding_index_identities
WHERE index_scope = 'corpus';

WITH inserted_document AS (
    INSERT INTO public.documents (
        filename,
        title,
        source,
        doc_type,
        chunk_count
    )
    VALUES (
        '__bm08_legacy__.md',
        'BM-08 legado',
        '__bm08_legacy__.md',
        'md',
        1
    )
    RETURNING id
)
INSERT INTO public.document_chunks (
    document_id,
    content,
    chunk_index,
    metadata,
    embedding,
    token_count
)
SELECT
    id,
    'vetor legado sem identidade conhecida',
    0,
    '{}'::jsonb,
    ARRAY(SELECT 0.0::REAL FROM generate_series(1, 1536))::vector,
    5
FROM inserted_document;

DO $$
BEGIN
    PERFORM public.ensure_embedding_index_identity(
        'corpus',
        'gemini',
        'gemini-embedding-001',
        1536,
        'rag-text-v1'
    );
    RAISE EXCEPTION 'Base legada sem identidade nao deveria ser identificada automaticamente.';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLERRM NOT LIKE 'Identidade vetorial ausente no escopo corpus,%' THEN
            RAISE;
        END IF;
END;
$$;

DO $$
BEGIN
    PERFORM public.ensure_embedding_index_identity(
        'feedback',
        'gemini',
        'gemini-embedding-001',
        3072,
        'rag-text-v1'
    );
    RAISE EXCEPTION 'Dimensao 3072 deveria ser rejeitada.';
EXCEPTION
    WHEN OTHERS THEN
        IF SQLERRM NOT LIKE 'Dimensao vetorial 3072 nao suportada.%' THEN
            RAISE;
        END IF;
END;
$$;

ROLLBACK;
