-- Identidade editorial canonica dos documentos e secoes.
-- Migracao aditiva: preserva dados legados e nao reindexa o corpus.

BEGIN;

ALTER TABLE public.documents
    ADD COLUMN IF NOT EXISTS canonical_id UUID NULL,
    ADD COLUMN IF NOT EXISTS schema_version TEXT NULL,
    ADD COLUMN IF NOT EXISTS document_revision INTEGER NULL,
    ADD COLUMN IF NOT EXISTS metadata JSONB NULL;

CREATE UNIQUE INDEX IF NOT EXISTS documents_canonical_id_unique
ON public.documents(canonical_id)
WHERE canonical_id IS NOT NULL;

ALTER TABLE public.document_sections
    ADD COLUMN IF NOT EXISTS section_key TEXT NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'public.documents'::regclass
          AND conname = 'documents_document_revision_positive_check'
    ) THEN
        ALTER TABLE public.documents
            ADD CONSTRAINT documents_document_revision_positive_check
            CHECK (document_revision IS NULL OR document_revision >= 1);
    END IF;
END;
$$;

CREATE UNIQUE INDEX IF NOT EXISTS document_sections_document_section_key_unique
ON public.document_sections(document_id, section_key)
WHERE section_key IS NOT NULL;

COMMIT;
