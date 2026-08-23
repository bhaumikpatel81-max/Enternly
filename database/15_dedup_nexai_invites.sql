-- Migration 15: De-duplicate nexai_invite rows
-- Each application should have at most one active invite in the tracker.
-- Multiple rows accumulate when recruiters re-send the invite repeatedly.
-- This migration deletes all but the most-recently-created invite per application.

DELETE FROM nexai_invite
WHERE id NOT IN (
    SELECT DISTINCT ON (application_id) id
    FROM nexai_invite
    ORDER BY application_id, invited_at DESC
);
