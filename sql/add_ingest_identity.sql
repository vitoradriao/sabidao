-- Identidade incremental da ingestao. A migracao e aditiva e nao reindexa dados.

ALTER TABLE documents
ADD COLUMN IF NOT EXISTS content_hash TEXT;

ALTER TABLE documents
ADD COLUMN IF NOT EXISTS processing_hash TEXT;

CREATE INDEX IF NOT EXISTS documents_content_hash_idx
ON documents(content_hash);

CREATE INDEX IF NOT EXISTS documents_processing_hash_idx
ON documents(processing_hash);
