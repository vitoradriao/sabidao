-- Identidade sanitizada dos dados efetivamente usados pelo benchmark offline.
-- A funcao calcula hashes no PostgreSQL e nao retorna conteudo do corpus ou feedback.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE OR REPLACE FUNCTION public.get_evaluation_data_identity()
RETURNS TABLE (
    schema_version INTEGER,
    corpus_document_count BIGINT,
    corpus_section_count BIGINT,
    corpus_chunk_count BIGINT,
    corpus_sha256 TEXT,
    feedback_item_count BIGINT,
    feedback_chunk_count BIGINT,
    feedback_sha256 TEXT,
    vector_index_identities JSONB
)
LANGUAGE sql
STABLE
AS $$
WITH document_manifest AS (
    SELECT ENCODE(
        DIGEST(
            CONCAT_WS(
                E'\x1f',
                d.filename,
                COALESCE(d.title, '<null>'),
                COALESCE(d.source, '<null>'),
                COALESCE(d.doc_type, '<null>'),
                COALESCE(d.content_hash, '<null>'),
                COALESCE(d.processing_hash, '<null>'),
                COALESCE(d.chunk_count::TEXT, '<null>'),
                COALESCE(d.priority::TEXT, '<null>')
            ),
            'sha256'
        ),
        'hex'
    ) AS row_sha256
    FROM public.documents d
),
section_manifest AS (
    SELECT ENCODE(
        DIGEST(
            CONCAT_WS(
                E'\x1f',
                d.filename,
                s.section_index::TEXT,
                s.heading_path,
                COALESCE(s.title, '<null>'),
                COALESCE(s.module, '<null>'),
                s.answer_mode,
                COALESCE(s.semantic_context, '<null>'),
                s.entities::TEXT,
                ENCODE(DIGEST(COALESCE(s.retrieval_text, ''), 'sha256'), 'hex'),
                ENCODE(DIGEST(COALESCE(s.embedding::TEXT, ''), 'sha256'), 'hex'),
                s.metadata::TEXT
            ),
            'sha256'
        ),
        'hex'
    ) AS row_sha256
    FROM public.document_sections s
    JOIN public.documents d ON d.id = s.document_id
),
chunk_manifest AS (
    SELECT ENCODE(
        DIGEST(
            CONCAT_WS(
                E'\x1f',
                d.filename,
                c.chunk_index::TEXT,
                COALESCE(s.section_index::TEXT, '<null>'),
                ENCODE(DIGEST(c.content, 'sha256'), 'hex'),
                c.metadata::TEXT,
                COALESCE(c.token_count::TEXT, '<null>'),
                COALESCE(c.module, '<null>'),
                COALESCE(c.doc_type, '<null>'),
                COALESCE(c.source_type, '<null>'),
                COALESCE(c.doc_priority::TEXT, '<null>'),
                COALESCE(c.heading_path, '<null>'),
                COALESCE(c.semantic_context, '<null>'),
                c.entities::TEXT,
                COALESCE(c.answer_mode, '<null>'),
                ENCODE(DIGEST(COALESCE(c.embedding::TEXT, ''), 'sha256'), 'hex')
            ),
            'sha256'
        ),
        'hex'
    ) AS row_sha256
    FROM public.document_chunks c
    JOIN public.documents d ON d.id = c.document_id
    LEFT JOIN public.document_sections s ON s.id = c.section_id
),
eligible_feedback AS (
    SELECT
        fi.id AS feedback_item_id,
        ENCODE(
            DIGEST(
                CONCAT_WS(
                    E'\x1f',
                    fc.scope::TEXT,
                    ENCODE(DIGEST(fc.content, 'sha256'), 'hex'),
                    ENCODE(DIGEST(COALESCE(fc.embedding::TEXT, ''), 'sha256'), 'hex')
                ),
                'sha256'
            ),
            'hex'
        ) AS row_sha256
    FROM public.feedback_chunks fc
    JOIN public.feedback_items fi ON fi.id = fc.feedback_item_id
    WHERE fc.active IS TRUE
      AND fi.status = 'PUBLISHED'
),
corpus_identity AS (
    SELECT ENCODE(
        DIGEST(
            'documents:' || COALESCE(
                (SELECT STRING_AGG(row_sha256, '' ORDER BY row_sha256) FROM document_manifest),
                ''
            ) ||
            '|sections:' || COALESCE(
                (SELECT STRING_AGG(row_sha256, '' ORDER BY row_sha256) FROM section_manifest),
                ''
            ) ||
            '|chunks:' || COALESCE(
                (SELECT STRING_AGG(row_sha256, '' ORDER BY row_sha256) FROM chunk_manifest),
                ''
            ),
            'sha256'
        ),
        'hex'
    ) AS sha256
),
feedback_identity AS (
    SELECT ENCODE(
        DIGEST(
            COALESCE(STRING_AGG(row_sha256, '' ORDER BY row_sha256), ''),
            'sha256'
        ),
        'hex'
    ) AS sha256
    FROM eligible_feedback
),
vector_identities AS (
    SELECT COALESCE(
        JSONB_AGG(
            JSONB_BUILD_OBJECT(
                'index_scope', identity.index_scope,
                'provider', identity.provider,
                'model', identity.model,
                'dimensions', identity.dimensions,
                'preprocessing_version', identity.preprocessing_version
            )
            ORDER BY identity.index_scope
        ),
        '[]'::JSONB
    ) AS identities
    FROM public.embedding_index_identities identity
)
SELECT
    1 AS schema_version,
    (SELECT COUNT(*) FROM public.documents) AS corpus_document_count,
    (SELECT COUNT(*) FROM public.document_sections) AS corpus_section_count,
    (SELECT COUNT(*) FROM public.document_chunks) AS corpus_chunk_count,
    corpus_identity.sha256 AS corpus_sha256,
    (SELECT COUNT(DISTINCT feedback_item_id) FROM eligible_feedback) AS feedback_item_count,
    (SELECT COUNT(*) FROM eligible_feedback) AS feedback_chunk_count,
    feedback_identity.sha256 AS feedback_sha256,
    vector_identities.identities AS vector_index_identities
FROM corpus_identity, feedback_identity, vector_identities;
$$;

COMMENT ON FUNCTION public.get_evaluation_data_identity() IS
'Retorna contagens, hashes sanitizados do corpus/feedback elegivel e identidades vetoriais persistidas para reproducao de benchmarks.';

NOTIFY pgrst, 'reload schema';
