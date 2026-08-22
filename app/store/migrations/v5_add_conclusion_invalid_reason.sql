PRAGMA foreign_keys = OFF;

BEGIN;

CREATE TABLE chapter_progress_v5 (
  research_id       TEXT NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
  goal_id           TEXT NOT NULL,
  chapter_id        TEXT NOT NULL,
  status             TEXT NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending','running','done','missing','deferred')),
  attempts           INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
  engine             TEXT CHECK (engine IN ('claude','codex') OR engine IS NULL),
  reason             TEXT CHECK (reason IN (
                       'empty_result','tool_unavailable','quota_exhausted','retry_exhausted',
                       'conclusion_invalid'
                     ) OR reason IS NULL),
  engine_error       TEXT,
  conclusion_error   TEXT,
  actual_output_path TEXT,
  actual_count       INTEGER CHECK (actual_count IS NULL OR actual_count >= 0),
  updated_at         TEXT NOT NULL,
  PRIMARY KEY (research_id, goal_id, chapter_id)
) STRICT;

INSERT INTO chapter_progress_v5 (
  research_id, goal_id, chapter_id, status, attempts, engine, reason,
  engine_error, conclusion_error, actual_output_path, actual_count, updated_at
)
SELECT
  research_id, goal_id, chapter_id, status, attempts, engine, reason,
  engine_error, conclusion_error, actual_output_path, actual_count, updated_at
FROM chapter_progress;

DROP TABLE chapter_progress;
ALTER TABLE chapter_progress_v5 RENAME TO chapter_progress;
CREATE INDEX idx_chapter_progress_status
ON chapter_progress(research_id, status);

PRAGMA user_version = 5;

COMMIT;

PRAGMA foreign_keys = ON;
