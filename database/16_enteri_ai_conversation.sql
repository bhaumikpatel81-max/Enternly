-- Migration 16: Add conversation column to enteri_ai_session
-- Stores the live [{speaker, text}] turn history for conversational (LLM) interview mode.
-- Scripted mode does not write to this column — existing questions/transcript columns
-- are left intact for full backward compatibility.

ALTER TABLE enteri_ai_session
    ADD COLUMN IF NOT EXISTS conversation JSONB;
