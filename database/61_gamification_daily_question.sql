-- ============================================================
-- Migration 61 (mirrors "Migration 98" in backend/app/main.py's
-- _auto_migrate() -- that function is the real source of truth;
-- this file is a documentation-only snapshot, per the convention
-- established for every migration after Migration 60).
--
-- Daily HR trivia question + per-subject answer streak.
-- ============================================================

-- 1. Question bank -----------------------------------------------
CREATE TABLE IF NOT EXISTS hr_question (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenant(id) DEFAULT '00000000-0000-0000-0000-000000000001',
    question_text    TEXT NOT NULL,
    option_a         TEXT NOT NULL,
    option_b         TEXT NOT NULL,
    option_c         TEXT NOT NULL,
    correct_option   TEXT NOT NULL CHECK (correct_option IN ('a','b','c')),
    explanation_text TEXT,
    active           BOOLEAN NOT NULL DEFAULT true,
    created_by       UUID REFERENCES app_user(id),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_hrq_tenant_active ON hr_question(tenant_id, active);

-- 2. One answer/skip row per subject per calendar day -------------
CREATE TABLE IF NOT EXISTS user_question_answer (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        UUID NOT NULL REFERENCES tenant(id) DEFAULT '00000000-0000-0000-0000-000000000001',
    subject_type     TEXT NOT NULL CHECK (subject_type IN ('recruiter','vendor','candidate','hm')),
    subject_id       UUID NOT NULL,
    question_id      UUID NOT NULL REFERENCES hr_question(id),
    answer_date      DATE NOT NULL,
    selected_option  TEXT CHECK (selected_option IN ('a','b','c')),  -- NULL when skipped
    is_correct       BOOLEAN,                                        -- NULL when skipped
    was_skipped      BOOLEAN NOT NULL DEFAULT false,
    points_awarded   NUMERIC NOT NULL DEFAULT 0,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (subject_type, subject_id, answer_date)
);

CREATE INDEX IF NOT EXISTS idx_uqa_subject_date ON user_question_answer(subject_type, subject_id, answer_date);

-- 3. Streak state --------------------------------------------------
-- Points/tier/rank stay derived from the gamification_event ledger
-- everywhere else in this codebase (see services/gamification.py) --
-- but a streak that must reset after a missed day needs sequential
-- date-gap bookkeeping that isn't expressible as a single aggregate.
-- This is a deliberate, narrow exception to the "derive, don't store"
-- convention: the only two writers are the answer and skip endpoints,
-- both already single-row transactions, so maintaining this at
-- write-time is cheap and avoids a growing window-function query on
-- every dashboard load.
CREATE TABLE IF NOT EXISTS user_gamification_streak (
    subject_type       TEXT NOT NULL CHECK (subject_type IN ('recruiter','vendor','candidate','hm')),
    subject_id         UUID NOT NULL,
    tenant_id          UUID NOT NULL REFERENCES tenant(id) DEFAULT '00000000-0000-0000-0000-000000000001',
    current_streak     INT NOT NULL DEFAULT 0,
    longest_streak     INT NOT NULL DEFAULT 0,
    last_activity_date DATE,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (subject_type, subject_id)
);

-- 4. Points config for a correct answer -----------------------------
INSERT INTO gamification_config (key, value) VALUES
  ('points.daily_question_correct', '10')
ON CONFLICT (tenant_id, key) DO NOTHING;
