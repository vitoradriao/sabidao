-- Identidade dos espacos vetoriais usados por corpus, secoes e feedback.
-- A migracao e aditiva: nao altera nem reindexa vetores existentes.

CREATE TABLE IF NOT EXISTS public.embedding_index_identities (
    index_scope TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    preprocessing_version TEXT NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    registered_by TEXT NOT NULL DEFAULT CURRENT_USER,
    CONSTRAINT embedding_index_identities_scope_check
        CHECK (index_scope IN ('corpus', 'sections', 'feedback')),
    CONSTRAINT embedding_index_identities_dimensions_check
        CHECK (dimensions = 1536),
    CONSTRAINT embedding_index_identities_values_check
        CHECK (
            BTRIM(provider) <> ''
            AND BTRIM(model) <> ''
            AND BTRIM(preprocessing_version) <> ''
        )
);

COMMENT ON TABLE public.embedding_index_identities IS
'Identidade imutavel do provider, modelo, dimensao e preprocessamento de cada colecao vetorial.';

CREATE OR REPLACE FUNCTION public.ensure_embedding_index_identity(
    p_index_scope TEXT,
    p_provider TEXT,
    p_model TEXT,
    p_dimensions INTEGER,
    p_preprocessing_version TEXT
)
RETURNS TABLE (
    index_scope TEXT,
    provider TEXT,
    model TEXT,
    dimensions INTEGER,
    preprocessing_version TEXT
)
LANGUAGE plpgsql
AS $$
DECLARE
    v_collection REGCLASS;
    v_vector_type TEXT;
    v_expected_vector_type TEXT := FORMAT('vector(%s)', p_dimensions);
    v_has_vectors BOOLEAN;
    v_existing public.embedding_index_identities%ROWTYPE;
    v_provider TEXT := LOWER(BTRIM(COALESCE(p_provider, '')));
    v_model TEXT := BTRIM(COALESCE(p_model, ''));
    v_preprocessing_version TEXT := BTRIM(COALESCE(p_preprocessing_version, ''));
BEGIN
    IF p_index_scope IS NULL
        OR p_index_scope NOT IN ('corpus', 'sections', 'feedback')
    THEN
        RAISE EXCEPTION
            'Escopo de indice vetorial invalido: %. Use corpus, sections ou feedback.',
            p_index_scope;
    END IF;

    IF v_provider = '' OR v_model = '' OR v_preprocessing_version = '' THEN
        RAISE EXCEPTION
            'Provider, modelo e versao de preprocessamento sao obrigatorios para o escopo %.',
            p_index_scope;
    END IF;

    IF p_dimensions IS DISTINCT FROM 1536 THEN
        RAISE EXCEPTION
            'Dimensao vetorial % nao suportada. Esta versao aceita somente 1536 dimensoes.',
            p_dimensions;
    END IF;

    v_collection := CASE p_index_scope
        WHEN 'corpus' THEN TO_REGCLASS('public.document_chunks')
        WHEN 'sections' THEN TO_REGCLASS('public.document_sections')
        WHEN 'feedback' THEN TO_REGCLASS('public.feedback_chunks')
    END;

    IF v_collection IS NULL THEN
        RAISE EXCEPTION
            'Colecao vetorial do escopo % nao existe. Aplique as migracoes SQL antes de iniciar.',
            p_index_scope;
    END IF;

    SELECT FORMAT_TYPE(attribute.atttypid, attribute.atttypmod)
    INTO v_vector_type
    FROM pg_attribute attribute
    WHERE attribute.attrelid = v_collection
      AND attribute.attname = 'embedding'
      AND NOT attribute.attisdropped;

    IF v_vector_type IS DISTINCT FROM v_expected_vector_type THEN
        RAISE EXCEPTION
            'Dimensao da coluna embedding incompativel no escopo %: banco usa %, processo espera %.',
            p_index_scope,
            COALESCE(v_vector_type, 'coluna ausente'),
            v_expected_vector_type;
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtextextended('embedding-index-identity:' || p_index_scope, 0)
    );

    SELECT identity.*
    INTO v_existing
    FROM public.embedding_index_identities identity
    WHERE identity.index_scope = p_index_scope
    FOR UPDATE;

    IF FOUND THEN
        IF v_existing.provider IS DISTINCT FROM v_provider
            OR v_existing.model IS DISTINCT FROM v_model
            OR v_existing.dimensions IS DISTINCT FROM p_dimensions
            OR v_existing.preprocessing_version IS DISTINCT FROM v_preprocessing_version
        THEN
            RAISE EXCEPTION
                'Identidade vetorial incompativel no escopo %: banco usa provider=%, model=%, dimensions=%, preprocessing_version=%; processo espera provider=%, model=%, dimensions=%, preprocessing_version=%.',
                p_index_scope,
                v_existing.provider,
                v_existing.model,
                v_existing.dimensions,
                v_existing.preprocessing_version,
                v_provider,
                v_model,
                p_dimensions,
                v_preprocessing_version;
        END IF;
    ELSE
        EXECUTE FORMAT(
            'SELECT EXISTS (SELECT 1 FROM %s WHERE embedding IS NOT NULL)',
            v_collection
        )
        INTO v_has_vectors;

        IF v_has_vectors THEN
            RAISE EXCEPTION
                'Identidade vetorial ausente no escopo %, mas a colecao ja contem vetores. Nao e seguro inferir provider ou modelo; siga docs/identidade-indice-vetorial.md.',
                p_index_scope;
        END IF;

        INSERT INTO public.embedding_index_identities (
            index_scope,
            provider,
            model,
            dimensions,
            preprocessing_version
        )
        VALUES (
            p_index_scope,
            v_provider,
            v_model,
            p_dimensions,
            v_preprocessing_version
        );
    END IF;

    RETURN QUERY
    SELECT
        identity.index_scope,
        identity.provider,
        identity.model,
        identity.dimensions,
        identity.preprocessing_version
    FROM public.embedding_index_identities identity
    WHERE identity.index_scope = p_index_scope;
END;
$$;

NOTIFY pgrst, 'reload schema';
