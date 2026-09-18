-- Tabelas para benchmark offline de qualidade de respostas RAG.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS evaluation_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_type TEXT NOT NULL DEFAULT 'offline',
    dataset_name TEXT NOT NULL,
    model TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    total_cases INTEGER NOT NULL DEFAULT 0,
    created_by TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS evaluation_runs_created_at_idx
ON evaluation_runs(created_at DESC);

CREATE TABLE IF NOT EXISTS evaluation_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES evaluation_runs(id) ON DELETE CASCADE,
    case_id TEXT NOT NULL,
    question TEXT NOT NULL,
    expected_behavior TEXT NOT NULL,
    expected_intent TEXT,
    predicted_intent TEXT,
    abstained BOOLEAN NOT NULL DEFAULT FALSE,
    behavior_match BOOLEAN,
    factual_correctness BOOLEAN,
    retrieval_relevance BOOLEAN,
    citation_validity BOOLEAN,
    citation_ok BOOLEAN,
    intent_match BOOLEAN,
    grounded BOOLEAN NOT NULL DEFAULT FALSE,
    top_similarity FLOAT,
    latency_ms INTEGER,
    score FLOAT,
    trace JSONB NOT NULL DEFAULT '{}'::jsonb,
    evaluation_details JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS evaluation_results_run_idx
ON evaluation_results(run_id);

CREATE INDEX IF NOT EXISTS evaluation_results_case_idx
ON evaluation_results(case_id);

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
