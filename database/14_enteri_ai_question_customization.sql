-- Migration 14: Per-requisition Enteri AI question editor
-- Run AFTER migrations 01–13.
-- Adds a table that stores a recruiter-edited fixed question set per requisition.
-- When a saved set exists, any new invite for that requisition uses it instead of
-- auto-generation, while requisitions never edited continue to auto-generate as before.

CREATE TABLE IF NOT EXISTS requisition_questions (
    requisition_id  UUID        PRIMARY KEY
                                REFERENCES requisition(id) ON DELETE CASCADE,
    questions       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    -- [{seq, text, expected_keywords:[...]}]  — same shape as enteri_ai_session.questions
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by      UUID        REFERENCES app_user(id)
);
