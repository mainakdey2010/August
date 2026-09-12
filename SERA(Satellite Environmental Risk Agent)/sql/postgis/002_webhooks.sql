-- Webhooks table — durable Postgres storage (replaces ephemeral Redis hashes)
-- Ensures webhook registrations survive Redis restarts.

CREATE TABLE IF NOT EXISTS webhooks (
  webhook_id   TEXT        PRIMARY KEY,
  url          TEXT        NOT NULL,
  secret_hash  TEXT        NOT NULL,    -- SHA-256 of the raw secret; never store plaintext
  events       TEXT[]      NOT NULL DEFAULT ARRAY['CRITICAL', 'HIGH'],
  region_ids   TEXT[]      NOT NULL DEFAULT '{}',   -- empty = all regions
  active       BOOL        NOT NULL DEFAULT TRUE,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_webhooks_active ON webhooks(active);
CREATE INDEX IF NOT EXISTS idx_webhooks_events ON webhooks USING GIN(events);

-- Note: secret_hash is SHA-256 of the raw secret.
-- Actual signing in delivery.py uses the raw secret fetched from Secret Manager,
-- not this hash. The hash is stored only for identifying duplicate registrations.
