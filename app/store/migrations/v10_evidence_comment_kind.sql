-- §CMT-1 货 3：评论作独立证据行入库，需要区分帖/评论并指回父帖。
ALTER TABLE evidence ADD COLUMN kind TEXT NOT NULL DEFAULT 'post'
  CHECK (kind IN ('post','comment'));
ALTER TABLE evidence ADD COLUMN parent_permalink TEXT;

CREATE INDEX IF NOT EXISTS idx_evidence_parent
  ON evidence(report_id, parent_permalink)
  WHERE parent_permalink IS NOT NULL;

PRAGMA user_version = 10;
