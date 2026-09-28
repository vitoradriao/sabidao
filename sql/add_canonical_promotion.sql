-- Promocao explicita de lotes canonicos. Nao altera o corpus existente.
CREATE TABLE IF NOT EXISTS public.canonical_migration_batches (
    batch_id TEXT PRIMARY KEY,
    plan_fingerprint TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('applying_manifest', 'applied', 'restoring_manifest', 'rolled_back')),
    original_snapshot_sha256 TEXT NOT NULL,
    before_manifest_sha256 TEXT NOT NULL,
    after_manifest_sha256 TEXT NOT NULL,
    before_manifest_text TEXT NOT NULL,
    after_manifest_text TEXT NOT NULL,
    before_corpus_sha256 TEXT NOT NULL,
    after_corpus_sha256 TEXT NOT NULL,
    after_rows_sha256 TEXT NOT NULL,
    before_rows JSONB NOT NULL,
    after_document_ids JSONB NOT NULL,
    archived_predecessors JSONB NOT NULL,
    successor_files JSONB NOT NULL,
    before_publication JSONB,
    aliases JSONB NOT NULL,
    counts JSONB NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    rolled_back_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS public.canonical_publication_state (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    batch_id TEXT NOT NULL REFERENCES public.canonical_migration_batches(batch_id),
    manifest_sha256 TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('applying_manifest', 'applied', 'restoring_manifest'))
);
