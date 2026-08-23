-- Migration 23: Extended application fields — employment snapshot, CTC, logistics
-- Informational only — these columns are captured for recruiter context and
-- DO NOT affect any screening score or algorithm.

ALTER TABLE application ADD COLUMN IF NOT EXISTS current_company       TEXT;
ALTER TABLE application ADD COLUMN IF NOT EXISTS current_designation   TEXT;
ALTER TABLE application ADD COLUMN IF NOT EXISTS current_location      TEXT;
ALTER TABLE application ADD COLUMN IF NOT EXISTS current_ctc_fixed     NUMERIC;
ALTER TABLE application ADD COLUMN IF NOT EXISTS current_ctc_variable  NUMERIC;
ALTER TABLE application ADD COLUMN IF NOT EXISTS current_ctc_bonus     NUMERIC;
ALTER TABLE application ADD COLUMN IF NOT EXISTS current_ctc_total     NUMERIC;
ALTER TABLE application ADD COLUMN IF NOT EXISTS expected_ctc_fixed    NUMERIC;
ALTER TABLE application ADD COLUMN IF NOT EXISTS expected_ctc_variable NUMERIC;
ALTER TABLE application ADD COLUMN IF NOT EXISTS expected_ctc_bonus    NUMERIC;
ALTER TABLE application ADD COLUMN IF NOT EXISTS expected_ctc_total    NUMERIC;
ALTER TABLE application ADD COLUMN IF NOT EXISTS notice_period_days    INTEGER;
ALTER TABLE application ADD COLUMN IF NOT EXISTS willing_to_relocate   BOOLEAN;

COMMENT ON COLUMN application.current_company      IS 'Current employer at time of application';
COMMENT ON COLUMN application.current_designation  IS 'Current job title at time of application';
COMMENT ON COLUMN application.current_location     IS 'Current city / location';
COMMENT ON COLUMN application.current_ctc_fixed    IS 'Current fixed annual salary (INR)';
COMMENT ON COLUMN application.current_ctc_variable IS 'Current variable pay (INR/year)';
COMMENT ON COLUMN application.current_ctc_bonus    IS 'Current bonus / allowances (INR/year)';
COMMENT ON COLUMN application.current_ctc_total    IS 'Auto-computed sum of current CTC components';
COMMENT ON COLUMN application.expected_ctc_fixed   IS 'Expected fixed salary (INR/year)';
COMMENT ON COLUMN application.expected_ctc_variable IS 'Expected variable pay (INR/year)';
COMMENT ON COLUMN application.expected_ctc_bonus   IS 'Expected bonus / allowances (INR/year)';
COMMENT ON COLUMN application.expected_ctc_total   IS 'Auto-computed sum of expected CTC components';
COMMENT ON COLUMN application.notice_period_days   IS 'Notice period in days at time of application';
COMMENT ON COLUMN application.willing_to_relocate  IS 'True = yes, False = no, NULL = not specified';
