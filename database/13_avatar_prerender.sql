-- Migration 13: Avatar pre-render pipeline
-- Run AFTER 01-12 migrations.
-- Adds per-question video tracking to nexai_session and a hash-based render cache.

-- ── Per-question video URLs and overall render status ─────────────────────────
-- question_videos: [{ "seq": 1, "question": "...", "video_url": "...", "status": "ready|rendering|failed|pending" }]
-- render_status: session-level summary of where pre-rendering stands.
ALTER TABLE nexai_session
  ADD COLUMN IF NOT EXISTS question_videos JSONB NOT NULL DEFAULT '[]'::jsonb,
  ADD COLUMN IF NOT EXISTS render_status   TEXT  NOT NULL DEFAULT 'pending'
    CHECK (render_status IN ('pending','rendering','ready','partial','failed'));

-- ── Avatar render cache ───────────────────────────────────────────────────────
-- cache_key = sha256(question_text | voice_id | avatar_image_name).
-- Identical questions across different sessions reuse the same MP4 — no re-render.
CREATE TABLE IF NOT EXISTS avatar_video_cache (
    cache_key   TEXT        PRIMARY KEY,
    gcs_url     TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
