-- 10: Support tickets & login activity log

CREATE TABLE IF NOT EXISTS support_ticket (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  raised_by   UUID NOT NULL REFERENCES app_user(id),
  category    TEXT NOT NULL DEFAULT 'other'
              CHECK (category IN ('login_issue','bug','data_issue','feature_request','other')),
  subject     TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  status      TEXT NOT NULL DEFAULT 'open'
              CHECK (status IN ('open','in_progress','resolved')),
  resolved_by UUID REFERENCES app_user(id),
  reply       TEXT,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  resolved_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS st_raised_by  ON support_ticket(raised_by);
CREATE INDEX IF NOT EXISTS st_status     ON support_ticket(status);
CREATE INDEX IF NOT EXISTS st_created_at ON support_ticket(created_at DESC);

-- Track each login for the admin system-health dashboard
CREATE TABLE IF NOT EXISTS login_log (
  id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id     UUID REFERENCES app_user(id),
  user_role   TEXT,
  ip_address  TEXT,
  logged_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ll_user      ON login_log(user_id);
CREATE INDEX IF NOT EXISTS ll_logged_at ON login_log(logged_at DESC);
