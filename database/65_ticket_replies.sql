-- support_ticket_reply (2026-08). Doc-only snapshot of "Migration 103" in
-- main.py's _auto_migrate(), which is what actually runs on every boot.
--
-- The platform console's cross-tenant Issues & Tickets screen (Feature G)
-- needs a threaded reply history on top of support_ticket's existing
-- single `reply` field.

CREATE TABLE IF NOT EXISTS support_ticket_reply (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ticket_id  UUID NOT NULL REFERENCES support_ticket(id),
    author_id  UUID NOT NULL REFERENCES app_user(id),
    body       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_ticket_reply_ticket ON support_ticket_reply(ticket_id, created_at);
