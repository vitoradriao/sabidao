\set ON_ERROR_STOP on

BEGIN;

DO $$
DECLARE
    before_row RECORD;
    corpus_row RECORD;
    feedback_row RECORD;
    repeated_row RECORD;
    document_id UUID := '00000000-0000-0000-0000-00000000e101';
    section_id UUID := '00000000-0000-0000-0000-00000000e102';
    feedback_id UUID := '00000000-0000-0000-0000-00000000e103';
BEGIN
    SELECT * INTO before_row FROM public.get_evaluation_data_identity();

    INSERT INTO public.documents (
        id, filename, title, source, doc_type, content_hash, processing_hash,
        chunk_count, priority
    ) VALUES (
        document_id, '__evaluation_identity_fixture__.md', 'Fixture', 'fixture',
        'manual', 'content-a', 'processing-a', 1, 5
    );

    INSERT INTO public.document_sections (
        id, document_id, section_index, heading_path, title, module,
        answer_mode, semantic_context, entities, metadata, retrieval_text, embedding
    ) VALUES (
        section_id, document_id, 0, 'Fixture', 'Fixture', 'fixture',
        'general', 'contexto', '{}'::jsonb, '{"fixture":true}'::jsonb,
        'texto de recuperacao', array_fill(0.0::real, ARRAY[1536])::vector
    );

    INSERT INTO public.document_chunks (
        document_id, content, chunk_index, metadata, embedding, token_count,
        section_id, heading_path, semantic_context, entities, answer_mode,
        module, doc_type, source_type, doc_priority
    ) VALUES (
        document_id, 'conteudo do trecho', 0, '{"fixture":true}'::jsonb,
        array_fill(0.0::real, ARRAY[1536])::vector, 3, section_id, 'Fixture',
        'contexto', '{}'::jsonb, 'general', 'fixture', 'manual', 'fixture', 5
    );

    SELECT * INTO corpus_row FROM public.get_evaluation_data_identity();
    IF corpus_row.corpus_sha256 = before_row.corpus_sha256 THEN
        RAISE EXCEPTION 'Fingerprint do corpus nao mudou apos inserir documento';
    END IF;
    IF corpus_row.corpus_document_count <> before_row.corpus_document_count + 1
       OR corpus_row.corpus_section_count <> before_row.corpus_section_count + 1
       OR corpus_row.corpus_chunk_count <> before_row.corpus_chunk_count + 1 THEN
        RAISE EXCEPTION 'Contagens do corpus nao refletem a fixture';
    END IF;

    INSERT INTO public.feedback_items (
        id, query, corrected_answer, scope, status, published_at
    ) VALUES (
        feedback_id, 'pergunta fixture', 'resposta fixture',
        '{"level":"global"}'::jsonb, 'PUBLISHED', NOW()
    );
    INSERT INTO public.feedback_chunks (
        feedback_item_id, content, scope, active, embedding
    ) VALUES (
        feedback_id, 'correcao publicada', '{"level":"global"}'::jsonb, TRUE,
        array_fill(0.0::real, ARRAY[1536])::vector
    );

    SELECT * INTO feedback_row FROM public.get_evaluation_data_identity();
    IF feedback_row.feedback_sha256 = corpus_row.feedback_sha256 THEN
        RAISE EXCEPTION 'Fingerprint do feedback nao mudou apos publicacao elegivel';
    END IF;
    IF feedback_row.feedback_item_count <> corpus_row.feedback_item_count + 1
       OR feedback_row.feedback_chunk_count <> corpus_row.feedback_chunk_count + 1 THEN
        RAISE EXCEPTION 'Contagens do feedback nao refletem a fixture';
    END IF;

    SELECT * INTO repeated_row FROM public.get_evaluation_data_identity();
    IF repeated_row.corpus_sha256 <> feedback_row.corpus_sha256
       OR repeated_row.feedback_sha256 <> feedback_row.feedback_sha256 THEN
        RAISE EXCEPTION 'Fingerprint equivalente nao e estavel';
    END IF;
END;
$$;

ROLLBACK;
