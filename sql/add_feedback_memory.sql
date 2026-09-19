-- Memoria de correcoes com fluxo revisado (PENDING -> APPROVED -> PUBLISHED)
-- e busca vetorial por escopo (global/tenant/erp/version/conversation).

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS feedback_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query TEXT NOT NULL,
    bot_answer TEXT,
    corrected_answer TEXT NOT NULL,
    tags JSONB NOT NULL DEFAULT '[]'::jsonb,
    scope JSONB NOT NULL DEFAULT '{"level":"global"}'::jsonb,
    status TEXT NOT NULL DEFAULT 'PENDING',
    platform TEXT,
    source_message_id TEXT,
    query_id UUID,
    created_by TEXT,
    reviewed_by TEXT,
    review_note TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reviewed_at TIMESTAMPTZ,
    published_at TIMESTAMPTZ,
    CONSTRAINT feedback_items_status_check
        CHECK (status IN ('PENDING', 'APPROVED', 'REJECTED', 'PUBLISHED'))
);

CREATE INDEX IF NOT EXISTS feedback_items_status_idx
ON feedback_items(status);

CREATE INDEX IF NOT EXISTS feedback_items_created_at_idx
ON feedback_items(created_at DESC);

CREATE INDEX IF NOT EXISTS feedback_items_scope_gin_idx
ON feedback_items USING gin(scope);

CREATE TABLE IF NOT EXISTS feedback_chunks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    feedback_item_id UUID NOT NULL REFERENCES feedback_items(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    scope JSONB NOT NULL DEFAULT '{"level":"global"}'::jsonb,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    embedding VECTOR(1536) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_by TEXT
);

CREATE INDEX IF NOT EXISTS feedback_chunks_item_idx
ON feedback_chunks(feedback_item_id);

CREATE INDEX IF NOT EXISTS feedback_chunks_active_idx
ON feedback_chunks(active)
WHERE active = TRUE;

CREATE INDEX IF NOT EXISTS feedback_chunks_scope_gin_idx
ON feedback_chunks USING gin(scope);

CREATE INDEX IF NOT EXISTS feedback_chunks_embedding_hnsw_idx
ON feedback_chunks
USING hnsw (embedding vector_cosine_ops)
WITH (m = 24, ef_construction = 128);

CREATE TABLE IF NOT EXISTS feedback_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    feedback_item_id UUID NOT NULL REFERENCES feedback_items(id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    actor TEXT,
    note TEXT,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS feedback_events_item_idx
ON feedback_events(feedback_item_id, created_at DESC);

CREATE TABLE IF NOT EXISTS documentation_update_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query TEXT NOT NULL,
    feedback_item_ids UUID[] NOT NULL DEFAULT '{}',
    base_sources TEXT[] NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN',
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ,
    CONSTRAINT documentation_update_tasks_status_check
        CHECK (status IN ('OPEN', 'DONE', 'CANCELED'))
);

CREATE INDEX IF NOT EXISTS documentation_update_tasks_status_idx
ON documentation_update_tasks(status, created_at DESC);

CREATE OR REPLACE FUNCTION public.submit_feedback(
    p_query TEXT,
    p_bot_answer TEXT,
    p_corrected_answer TEXT,
    p_tags JSONB DEFAULT '[]'::jsonb,
    p_scope JSONB DEFAULT '{"level":"global"}'::jsonb,
    p_created_by TEXT DEFAULT NULL,
    p_platform TEXT DEFAULT NULL,
    p_source_message_id TEXT DEFAULT NULL,
    p_query_id UUID DEFAULT NULL,
    p_metadata JSONB DEFAULT '{}'::jsonb
)
RETURNS UUID
LANGUAGE plpgsql
AS $$
DECLARE
    v_id UUID;
BEGIN
    INSERT INTO feedback_items (
        query,
        bot_answer,
        corrected_answer,
        tags,
        scope,
        status,
        created_by,
        platform,
        source_message_id,
        query_id,
        metadata
    )
    VALUES (
        p_query,
        p_bot_answer,
        p_corrected_answer,
        COALESCE(p_tags, '[]'::jsonb),
        COALESCE(p_scope, '{"level":"global"}'::jsonb),
        'PENDING',
        p_created_by,
        p_platform,
        p_source_message_id,
        p_query_id,
        COALESCE(p_metadata, '{}'::jsonb)
    )
    RETURNING id INTO v_id;

    INSERT INTO feedback_events (feedback_item_id, event_type, actor, payload)
    VALUES (
        v_id,
        'SUBMITTED',
        p_created_by,
        jsonb_build_object('platform', p_platform, 'query_id', p_query_id)
    );

    RETURN v_id;
END;
$$;

CREATE OR REPLACE FUNCTION public.list_pending_feedback(
    p_limit INT DEFAULT 20
)
RETURNS TABLE (
    id UUID,
    query TEXT,
    corrected_answer TEXT,
    tags JSONB,
    scope JSONB,
    created_by TEXT,
    platform TEXT,
    created_at TIMESTAMPTZ
)
LANGUAGE sql
AS $$
    SELECT
        fi.id,
        fi.query,
        fi.corrected_answer,
        fi.tags,
        fi.scope,
        fi.created_by,
        fi.platform,
        fi.created_at
    FROM feedback_items fi
    WHERE fi.status = 'PENDING'
    ORDER BY fi.created_at ASC
    LIMIT GREATEST(1, LEAST(p_limit, 200));
$$;

CREATE OR REPLACE FUNCTION public.approve_feedback(
    p_id UUID,
    p_reviewer TEXT DEFAULT NULL,
    p_note TEXT DEFAULT NULL
)
RETURNS VOID
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE feedback_items
    SET
        status = 'APPROVED',
        reviewed_by = p_reviewer,
        review_note = p_note,
        reviewed_at = NOW()
    WHERE id = p_id
      AND status IN ('PENDING', 'REJECTED');

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Feedback % nao encontrado para aprovacao.', p_id;
    END IF;

    INSERT INTO feedback_events (feedback_item_id, event_type, actor, note)
    VALUES (p_id, 'APPROVED', p_reviewer, p_note);
END;
$$;

CREATE OR REPLACE FUNCTION public.reject_feedback(
    p_id UUID,
    p_reviewer TEXT DEFAULT NULL,
    p_note TEXT DEFAULT NULL
)
RETURNS VOID
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE feedback_items
    SET
        status = 'REJECTED',
        reviewed_by = p_reviewer,
        review_note = p_note,
        reviewed_at = NOW()
    WHERE id = p_id
      AND status IN ('PENDING', 'APPROVED');

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Feedback % nao encontrado para rejeicao.', p_id;
    END IF;

    INSERT INTO feedback_events (feedback_item_id, event_type, actor, note)
    VALUES (p_id, 'REJECTED', p_reviewer, p_note);
END;
$$;

CREATE OR REPLACE FUNCTION public.publish_feedback(
    p_id UUID,
    p_actor TEXT DEFAULT NULL,
    p_chunk_text TEXT DEFAULT NULL,
    p_scope_override JSONB DEFAULT NULL,
    p_embedding VECTOR(1536) DEFAULT NULL
)
RETURNS UUID
LANGUAGE plpgsql
AS $$
DECLARE
    v_item feedback_items%ROWTYPE;
    v_chunk_id UUID;
BEGIN
    IF p_embedding IS NULL THEN
        RAISE EXCEPTION 'publish_feedback requer p_embedding.';
    END IF;

    SELECT *
    INTO v_item
    FROM feedback_items
    WHERE id = p_id
    FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'Feedback % nao encontrado para publicacao.', p_id;
    END IF;

    IF v_item.status NOT IN ('APPROVED', 'PUBLISHED') THEN
        RAISE EXCEPTION 'Feedback % precisa estar APPROVED antes de publicar.', p_id;
    END IF;

    INSERT INTO feedback_chunks (
        feedback_item_id,
        content,
        scope,
        active,
        embedding,
        published_by
    )
    VALUES (
        v_item.id,
        COALESCE(NULLIF(TRIM(p_chunk_text), ''), v_item.corrected_answer),
        COALESCE(p_scope_override, v_item.scope),
        TRUE,
        p_embedding,
        p_actor
    )
    RETURNING id INTO v_chunk_id;

    UPDATE feedback_items
    SET
        status = 'PUBLISHED',
        published_at = NOW(),
        reviewed_by = COALESCE(reviewed_by, p_actor)
    WHERE id = v_item.id;

    INSERT INTO feedback_events (feedback_item_id, event_type, actor, payload)
    VALUES (
        v_item.id,
        'PUBLISHED',
        p_actor,
        jsonb_build_object('feedback_chunk_id', v_chunk_id)
    );

    RETURN v_chunk_id;
END;
$$;

CREATE OR REPLACE FUNCTION public.search_feedback_chunks(
    query_embedding VECTOR(1536),
    match_count INT DEFAULT 6,
    match_threshold FLOAT DEFAULT 0.58,
    scope_level TEXT DEFAULT NULL,
    scope_tenant TEXT DEFAULT NULL,
    scope_erp TEXT DEFAULT NULL,
    scope_version TEXT DEFAULT NULL
)
RETURNS TABLE (
    id UUID,
    feedback_item_id UUID,
    content TEXT,
    scope JSONB,
    similarity FLOAT
)
LANGUAGE sql
AS $$
    SELECT
        fc.id,
        fc.feedback_item_id,
        fc.content,
        fc.scope,
        (1 - (fc.embedding <=> query_embedding))::FLOAT AS similarity
    FROM feedback_chunks fc
    JOIN feedback_items fi ON fi.id = fc.feedback_item_id
    WHERE fc.active = TRUE
      AND fi.status = 'PUBLISHED'
      AND (
          scope_level IS NULL
          OR COALESCE(fc.scope->>'level', 'global') = scope_level
      )
      AND (
          scope_tenant IS NULL
          OR COALESCE(fc.scope->>'tenant', '') = scope_tenant
      )
      AND (
          scope_erp IS NULL
          OR COALESCE(fc.scope->>'erp', '') = scope_erp
      )
      AND (
          scope_version IS NULL
          OR COALESCE(fc.scope->>'version', '') = scope_version
      )
      AND 1 - (fc.embedding <=> query_embedding) >= match_threshold
    ORDER BY fc.embedding <=> query_embedding ASC
    LIMIT GREATEST(1, LEAST(match_count, 50));
$$;

CREATE OR REPLACE FUNCTION public.search_feedback_chunks_scoped(
    query_embedding VECTOR(1536),
    scope_filter JSONB,
    match_count INT DEFAULT 6,
    match_threshold FLOAT DEFAULT 0.58
)
RETURNS TABLE (
    id UUID,
    feedback_item_id UUID,
    content TEXT,
    scope JSONB,
    similarity FLOAT
)
LANGUAGE sql
AS $$
    SELECT
        fc.id,
        fc.feedback_item_id,
        fc.content,
        fc.scope,
        (1 - (fc.embedding <=> query_embedding))::FLOAT AS similarity
    FROM feedback_chunks fc
    JOIN feedback_items fi ON fi.id = fc.feedback_item_id
    WHERE fc.active = TRUE
      AND fi.status = 'PUBLISHED'
      AND fc.scope = scope_filter
      AND 1 - (fc.embedding <=> query_embedding) >= match_threshold
    ORDER BY fc.embedding <=> query_embedding ASC
    LIMIT GREATEST(1, LEAST(match_count, 50));
$$;

CREATE OR REPLACE FUNCTION public.create_documentation_update_task(
    p_query TEXT,
    p_feedback_item_ids UUID[] DEFAULT '{}',
    p_base_sources TEXT[] DEFAULT '{}',
    p_reason TEXT DEFAULT 'Feedback escopado divergiu da base principal',
    p_metadata JSONB DEFAULT '{}'::jsonb
)
RETURNS UUID
LANGUAGE plpgsql
AS $$
DECLARE
    v_task_id UUID;
BEGIN
    INSERT INTO documentation_update_tasks (
        query,
        feedback_item_ids,
        base_sources,
        reason,
        status,
        metadata
    )
    VALUES (
        p_query,
        COALESCE(p_feedback_item_ids, '{}'),
        COALESCE(p_base_sources, '{}'),
        p_reason,
        'OPEN',
        COALESCE(p_metadata, '{}'::jsonb)
    )
    RETURNING id INTO v_task_id;

    RETURN v_task_id;
END;
$$;

NOTIFY pgrst, 'reload schema';
