CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_native_identity
  ON evidence(report_id, platform, platform_item_id)
  WHERE platform_item_id IS NOT NULL AND platform_item_id <> '';

PRAGMA user_version = 9;
