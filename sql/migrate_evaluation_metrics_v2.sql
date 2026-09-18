-- Atualiza instalacoes existentes para o contrato de avaliacao v2.
-- Valores NULL significam que a metrica nao pôde ser avaliada.

ALTER TABLE evaluation_results
    ADD COLUMN IF NOT EXISTS behavior_match BOOLEAN,
    ADD COLUMN IF NOT EXISTS factual_correctness BOOLEAN,
    ADD COLUMN IF NOT EXISTS retrieval_relevance BOOLEAN,
    ADD COLUMN IF NOT EXISTS citation_validity BOOLEAN,
    ADD COLUMN IF NOT EXISTS intent_match BOOLEAN,
    ADD COLUMN IF NOT EXISTS evaluation_details JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE evaluation_results
    ALTER COLUMN citation_ok DROP NOT NULL,
    ALTER COLUMN citation_ok DROP DEFAULT,
    ALTER COLUMN score DROP NOT NULL,
    ALTER COLUMN score DROP DEFAULT;

CREATE OR REPLACE VIEW evaluation_run_summary AS
SELECT
    r.id AS run_id,
    r.dataset_name,
    r.model,
    r.embedding_model,
    r.total_cases,
    r.created_at,
    AVG(er.score)::FLOAT AS avg_score,
    AVG(CASE WHEN er.grounded THEN 1 ELSE 0 END)::FLOAT AS grounded_rate,
    AVG(CASE WHEN er.citation_validity THEN 1 WHEN NOT er.citation_validity THEN 0 END)::FLOAT
        AS citation_ok_rate,
    AVG(CASE WHEN er.abstained THEN 1 ELSE 0 END)::FLOAT AS abstain_rate,
    AVG(CASE WHEN er.factual_correctness THEN 1 WHEN NOT er.factual_correctness THEN 0 END)::FLOAT
        AS factual_correctness_rate,
    AVG(CASE WHEN er.retrieval_relevance THEN 1 WHEN NOT er.retrieval_relevance THEN 0 END)::FLOAT
        AS retrieval_relevance_rate,
    AVG(CASE WHEN er.behavior_match THEN 1 WHEN NOT er.behavior_match THEN 0 END)::FLOAT
        AS behavior_match_rate,
    AVG(CASE WHEN er.intent_match THEN 1 WHEN NOT er.intent_match THEN 0 END)::FLOAT
        AS intent_accuracy,
    COUNT(er.score) AS score_evaluated,
    COUNT(er.factual_correctness) AS factual_correctness_evaluated,
    COUNT(er.retrieval_relevance) AS retrieval_relevance_evaluated,
    COUNT(er.citation_validity) AS citation_validity_evaluated
FROM evaluation_runs r
LEFT JOIN evaluation_results er ON er.run_id = r.id
GROUP BY r.id, r.dataset_name, r.model, r.embedding_model, r.total_cases, r.created_at;

NOTIFY pgrst, 'reload schema';
