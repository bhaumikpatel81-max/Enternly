-- ============================================================
-- ONE CLICK HIRE  -  Seed data
-- Bands, approval chains, default feedback form, email templates.
-- Group Companies and Business Units are NOT seeded here — they
-- are created by TA Admin / TA Manager via Settings → Organisation.
-- Run AFTER 01_schema.sql.
-- ============================================================

-- ---------- BANDS (lowest rank 1 -> highest rank 13) ----------
INSERT INTO band (code, rank, description) VALUES
  ('5',  1,  'Entry / blue-collar / fresher'),
  ('4C', 2,  'Junior'),
  ('4B', 3,  'Junior'),
  ('4A', 4,  'Associate'),
  ('3D', 5,  'Executive'),
  ('3C', 6,  'Executive'),
  ('3B', 7,  'Senior executive'),
  ('3A', 8,  'Lead'),
  ('2C', 9,  'Manager'),
  ('2B', 10, 'Senior manager'),
  ('2A', 11, 'AGM'),
  ('1B', 12, 'GM / VP'),
  ('1A', 13, 'Senior leadership');

-- ---------- USERS ----------
-- Only the TA Admin seed account. Password is NOT set here.
-- Admin must use "Forgot password" on first login to set their password.
INSERT INTO app_user (full_name, email, role) VALUES
  ('TA Admin', 'hr@amnex.com', 'admin')
ON CONFLICT (email) DO NOTHING;

-- ---------- APPROVAL CHAINS (per band group) ----------
-- Junior bands: BU head only. Senior bands: BU head + director.
INSERT INTO approval_chain (band_id, name, approver_steps)
SELECT b.id,
       'Junior chain (' || b.code || ')',
       '[{"step":1,"role":"bu_head"}]'::jsonb
FROM band b WHERE b.rank <= 5;

INSERT INTO approval_chain (band_id, name, approver_steps)
SELECT b.id,
       'Senior chain (' || b.code || ')',
       '[{"step":1,"role":"bu_head"},{"step":2,"role":"director"}]'::jsonb
FROM band b WHERE b.rank > 5;

-- ---------- FEEDBACK FORM (default panel scorecard) ----------
INSERT INTO feedback_form (name, schema, created_by)
SELECT 'Default panel scorecard',
       '[
         {"key":"technical","label":"Technical skills","type":"rating_5"},
         {"key":"communication","label":"Communication","type":"rating_5"},
         {"key":"culture_fit","label":"Culture fit","type":"rating_5"},
         {"key":"comments","label":"Comments","type":"text"}
       ]'::jsonb,
       u.id
FROM app_user u WHERE u.email = 'hr@amnex.com';

-- ---------- EMAIL TEMPLATES (customizable) ----------
INSERT INTO email_template (name, subject, body, category, created_by)
SELECT t.name, t.subject, t.body, t.category, u.id
FROM app_user u
CROSS JOIN (VALUES
  ('Interview invite (candidate)',
   'Interview scheduled: {{job_title}} at EnternsTech',
   'Dear {{candidate_name}},

Your interview for {{job_title}} is scheduled on {{interview_time}}.
Meeting link: {{meet_link}}

Regards,
EnternsTech Talent Acquisition', 'candidate'),
  ('Panel notification',
   'Interview assigned: {{candidate_name}} for {{job_title}}',
   'Hi {{panel_name}},

You are scheduled to interview {{candidate_name}} for {{job_title}} on {{interview_time}}.
Link: {{meet_link}}', 'panel')
) AS t(name, subject, body, category)
WHERE u.email = 'hr@amnex.com';
