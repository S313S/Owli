CREATE TABLE source_usage (
  source     TEXT NOT NULL,
  utc_date   TEXT NOT NULL,
  reads      INTEGER NOT NULL DEFAULT 0 CHECK (reads >= 0),
  requests   INTEGER NOT NULL DEFAULT 0 CHECK (requests >= 0),
  PRIMARY KEY (source, utc_date)
) STRICT;

CREATE TABLE source_usage_billed_resource (
  source      TEXT NOT NULL,
  utc_date    TEXT NOT NULL,
  resource_id TEXT NOT NULL,
  PRIMARY KEY (source, utc_date, resource_id)
) STRICT;

PRAGMA user_version = 2;
