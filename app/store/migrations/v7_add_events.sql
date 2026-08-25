CREATE TABLE IF NOT EXISTS events (
  research_id TEXT NOT NULL,
  sequence    INTEGER NOT NULL CHECK (sequence > 0),
  type        TEXT NOT NULL,
  payload     TEXT NOT NULL CHECK (json_valid(payload)),
  created_at  TEXT NOT NULL,
  PRIMARY KEY (research_id, sequence)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_events_research_created
ON events(research_id, created_at, sequence);

PRAGMA user_version = 7;
