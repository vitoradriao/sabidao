\set ON_ERROR_STOP on

BEGIN;

INSERT INTO feedback_items (
    id,
    query,
    corrected_answer,
    scope,
    status
)
VALUES
    (
        '00000000-0000-0000-0000-0000000000a1',
        'pergunta A',
        'resposta A',
        '{"level":"conversation","platform":"discord","guild_id":"1","channel_id":"10"}'::jsonb,
        'PUBLISHED'
    ),
    (
        '00000000-0000-0000-0000-0000000000b1',
        'pergunta B',
        'resposta B',
        '{"level":"conversation","platform":"discord","guild_id":"1","channel_id":"20"}'::jsonb,
        'PUBLISHED'
    ),
    (
        '00000000-0000-0000-0000-0000000000c1',
        'pergunta global',
        'resposta global',
        '{"level":"global"}'::jsonb,
        'PUBLISHED'
    );

INSERT INTO feedback_chunks (
    feedback_item_id,
    content,
    scope,
    embedding
)
SELECT
    item.id,
    item.corrected_answer,
    item.scope,
    array_fill(0.1::real, ARRAY[1536])::vector
FROM feedback_items item
WHERE item.id IN (
    '00000000-0000-0000-0000-0000000000a1',
    '00000000-0000-0000-0000-0000000000b1',
    '00000000-0000-0000-0000-0000000000c1'
);

DO $$
DECLARE
    query_vector vector(1536) := array_fill(0.1::real, ARRAY[1536])::vector;
    conversation_a_count INT;
    conversation_b_count INT;
    global_count INT;
BEGIN
    SELECT COUNT(*)
    INTO conversation_a_count
    FROM search_feedback_chunks_scoped(
        query_vector,
        '{"level":"conversation","platform":"discord","guild_id":"1","channel_id":"10"}'::jsonb,
        10,
        0.0
    );

    SELECT COUNT(*)
    INTO conversation_b_count
    FROM search_feedback_chunks_scoped(
        query_vector,
        '{"level":"conversation","platform":"discord","guild_id":"1","channel_id":"20"}'::jsonb,
        10,
        0.0
    );

    SELECT COUNT(*)
    INTO global_count
    FROM search_feedback_chunks(
        query_vector,
        10,
        0.0,
        'global'
    );

    IF conversation_a_count <> 1 OR conversation_b_count <> 1 THEN
        RAISE EXCEPTION
            'Busca de feedback vazou entre conversas: A=%, B=%',
            conversation_a_count,
            conversation_b_count;
    END IF;

    IF global_count <> 1 THEN
        RAISE EXCEPTION 'Busca global perdeu semantica explicita: %', global_count;
    END IF;
END;
$$;

ROLLBACK;
