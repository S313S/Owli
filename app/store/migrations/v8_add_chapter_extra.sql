ALTER TABLE chapter_progress
ADD COLUMN extra TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(extra));

PRAGMA user_version = 8;
