-- Migration: split budgeted_ctc into fixed + variable components
-- Run AFTER 01_schema.sql

ALTER TABLE requisition
  ADD COLUMN IF NOT EXISTS budgeted_fixed    NUMERIC,
  ADD COLUMN IF NOT EXISTS budgeted_variable NUMERIC;
