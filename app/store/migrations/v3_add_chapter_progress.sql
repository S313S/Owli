CREATE TABLE IF NOT EXISTS chapter_progress (
  research_id       TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
  goal_id           TEXT NOT NULL,
  chapter_id        TEXT NOT NULL,
  status            TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','running','done','missing','deferred')),
  attempts          INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  engine            TEXT CHECK (engine IN ('claude','codex') OR engine IS NULL),
  reason            TEXT CHECK (reason IN (
                      'empty_result','tool_unavailable','quota_exhausted','retry_exhausted'
                    ) OR reason IS NULL),
  actual_output_path TEXT,
  actual_count       INTEGER CHECK (actual_count IS NULL OR actual_count >= 0),
  updated_at         TEXT NOT NULL,
  PRIMARY KEY (research_id, goal_id, chapter_id)
) STRICT;

CREATE INDEX IF NOT EXISTS idx_chapter_progress_status
ON chapter_progress(research_id, status);

PRAGMA user_version = 3;
