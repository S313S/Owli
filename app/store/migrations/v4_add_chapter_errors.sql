ALTER TABLE chapter_progress ADD COLUMN engine_error TEXT;
ALTER TABLE chapter_progress ADD COLUMN conclusion_error TEXT;

PRAGMA user_version = 4;
