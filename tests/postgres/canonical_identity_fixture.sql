BEGIN;

DO $$
DECLARE
    legacy_document_id UUID := '00000000-0000-0000-0000-000000000661';
    mixed_document_id UUID := '00000000-0000-0000-0000-000000000662';
    rollback_document_id UUID := '00000000-0000-0000-0000-000000000663';
    canonical_document_id UUID := '00000000-0000-0000-0000-000000000664';
    legacy_row RECORD;
    mixed_row RECORD;
    section_count INTEGER;
    duplicate_rejected BOOLEAN;
    column_type TEXT;
    nullable_flag TEXT;
BEGIN
    SELECT
        information_schema.columns.data_type,
        information_schema.columns.is_nullable
    INTO column_type, nullable_flag
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'documents'
      AND column_name = 'canonical_id';

    IF column_type IS DISTINCT FROM 'uuid'
       OR nullable_flag IS DISTINCT FROM 'YES' THEN
        RAISE EXCEPTION
            'documents.canonical_id deveria ser UUID nullable; obtido tipo=%, nullable=%',
            column_type,
            nullable_flag;
    END IF;

    SELECT data_type, is_nullable
    INTO column_type, nullable_flag
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'documents'
      AND column_name = 'schema_version';

    IF column_type IS DISTINCT FROM 'text'
       OR nullable_flag IS DISTINCT FROM 'YES' THEN
        RAISE EXCEPTION
            'documents.schema_version deveria ser TEXT nullable; obtido tipo=%, nullable=%',
            column_type,
            nullable_flag;
    END IF;

    SELECT data_type, is_nullable
    INTO column_type, nullable_flag
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'documents'
      AND column_name = 'document_revision';

    IF column_type IS DISTINCT FROM 'integer'
       OR nullable_flag IS DISTINCT FROM 'YES' THEN
        RAISE EXCEPTION
            'documents.document_revision deveria ser INTEGER nullable; obtido tipo=%, nullable=%',
            column_type,
            nullable_flag;
    END IF;

    SELECT data_type, is_nullable
    INTO column_type, nullable_flag
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'documents'
      AND column_name = 'metadata';

    IF column_type IS DISTINCT FROM 'jsonb'
       OR nullable_flag IS DISTINCT FROM 'YES' THEN
        RAISE EXCEPTION
            'documents.metadata deveria ser JSONB nullable; obtido tipo=%, nullable=%',
            column_type,
            nullable_flag;
    END IF;

    SELECT data_type, is_nullable
    INTO column_type, nullable_flag
    FROM information_schema.columns
    WHERE table_schema = 'public'
      AND table_name = 'document_sections'
      AND column_name = 'section_key';

    IF column_type IS DISTINCT FROM 'text'
       OR nullable_flag IS DISTINCT FROM 'YES' THEN
        RAISE EXCEPTION
            'document_sections.section_key deveria ser TEXT nullable; obtido tipo=%, nullable=%',
            column_type,
            nullable_flag;
    END IF;

    INSERT INTO public.documents (
        id, filename, doc_type, content_hash, processing_hash
    )
    VALUES (
        legacy_document_id,
        '__canonical_identity_66_legacy__.md',
        'md',
        'legacy-content-signature',
        'legacy-processing-signature'
    );

    INSERT INTO public.documents (
        id,
        filename,
        doc_type,
        canonical_id,
        schema_version,
        document_revision,
        metadata
    )
    VALUES (
        mixed_document_id,
        '__canonical_identity_66_mixed__.md',
        'manual',
        canonical_document_id,
        'canonical-doc/v1',
        3,
        '{"origin":"fixture","published":true}'::jsonb
    );

    SELECT
        filename,
        doc_type,
        content_hash,
        processing_hash,
        canonical_id,
        schema_version,
        document_revision,
        metadata
    INTO legacy_row
    FROM public.documents
    WHERE id = legacy_document_id;

    IF legacy_row.filename <> '__canonical_identity_66_legacy__.md'
       OR legacy_row.doc_type <> 'md'
       OR legacy_row.content_hash <> 'legacy-content-signature'
       OR legacy_row.processing_hash <> 'legacy-processing-signature'
       OR legacy_row.canonical_id IS NOT NULL
       OR legacy_row.schema_version IS NOT NULL
       OR legacy_row.document_revision IS NOT NULL
       OR legacy_row.metadata IS NOT NULL THEN
        RAISE EXCEPTION 'Dados legados nao foram preservados como nulos.';
    END IF;

    SELECT filename, doc_type, canonical_id, schema_version, document_revision, metadata
    INTO mixed_row
    FROM public.documents
    WHERE id = mixed_document_id;

    IF mixed_row.filename <> '__canonical_identity_66_mixed__.md'
       OR mixed_row.doc_type <> 'manual'
       OR mixed_row.canonical_id <> canonical_document_id
       OR mixed_row.schema_version <> 'canonical-doc/v1'
       OR mixed_row.document_revision <> 3
       OR mixed_row.metadata <> '{"origin":"fixture","published":true}'::jsonb THEN
        RAISE EXCEPTION 'Dados mistos nao foram preservados.';
    END IF;

    duplicate_rejected := FALSE;
    BEGIN
        INSERT INTO public.documents (
            id, filename, doc_type, document_revision
        )
        VALUES (
            rollback_document_id,
            '__canonical_identity_66_invalid_revision__.md',
            'manual',
            0
        );
    EXCEPTION
        WHEN check_violation THEN
            duplicate_rejected := TRUE;
    END;

    IF NOT duplicate_rejected THEN
        RAISE EXCEPTION 'document_revision zero deveria ser rejeitado.';
    END IF;

    BEGIN
        INSERT INTO public.documents (id, filename, doc_type)
        VALUES (rollback_document_id, '__canonical_identity_66_rollback__.md', 'md');
        RAISE EXCEPTION 'rollback_probe';
    EXCEPTION
        WHEN SQLSTATE 'P0001' THEN
            NULL;
    END;

    IF EXISTS (
        SELECT 1
        FROM public.documents
        WHERE id = rollback_document_id
    ) THEN
        RAISE EXCEPTION 'Rollback transacional deixou dado de fixture persistido.';
    END IF;

    duplicate_rejected := FALSE;
    BEGIN
        INSERT INTO public.documents (
            id, filename, doc_type, canonical_id
        )
        VALUES (
            rollback_document_id,
            '__canonical_identity_66_duplicate__.md',
            'manual',
            canonical_document_id
        );
    EXCEPTION
        WHEN unique_violation THEN
            duplicate_rejected := TRUE;
    END;

    IF NOT duplicate_rejected THEN
        RAISE EXCEPTION 'canonical_id duplicado deveria ser rejeitado.';
    END IF;

    INSERT INTO public.document_sections (
        document_id, section_index, section_key
    )
    VALUES (legacy_document_id, 0, 'visao-geral');

    INSERT INTO public.document_sections (
        document_id, section_index, section_key
    )
    VALUES (mixed_document_id, 0, 'visao-geral');

    INSERT INTO public.document_sections (
        document_id, section_index, section_key
    )
    VALUES (legacy_document_id, 1, NULL);

    INSERT INTO public.document_sections (
        document_id, section_index, section_key
    )
    VALUES (legacy_document_id, 2, NULL);

    SELECT COUNT(*)
    INTO section_count
    FROM public.document_sections
    WHERE section_key = 'visao-geral';

    IF section_count <> 2 THEN
        RAISE EXCEPTION
            'A mesma section_key deveria ser aceita em documentos distintos; obtidas % linhas.',
            section_count;
    END IF;

    duplicate_rejected := FALSE;
    BEGIN
        INSERT INTO public.document_sections (
            document_id, section_index, section_key
        )
        VALUES (legacy_document_id, 3, 'visao-geral');
    EXCEPTION
        WHEN unique_violation THEN
            duplicate_rejected := TRUE;
    END;

    IF NOT duplicate_rejected THEN
        RAISE EXCEPTION
            'section_key duplicada no mesmo documento deveria ser rejeitada.';
    END IF;
END;
$$;

ROLLBACK;
