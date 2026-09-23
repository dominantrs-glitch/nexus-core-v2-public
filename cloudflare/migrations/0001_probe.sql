-- Synthetic read-only proof only. No remote write/admin MCP tools.
CREATE TABLE projects (id TEXT PRIMARY KEY);
CREATE TABLE grants (
  issuer TEXT NOT NULL, subject TEXT NOT NULL, client_id TEXT NOT NULL,
  project TEXT NOT NULL REFERENCES projects(id),
  PRIMARY KEY (issuer, subject, client_id, project)
);
CREATE TABLE artifacts (
  id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(id)
);
CREATE TABLE revisions (
  artifact_id TEXT NOT NULL REFERENCES artifacts(id),
  revision INTEGER NOT NULL CHECK (revision > 0),
  mime TEXT NOT NULL CHECK (mime IN ('image/png', 'text/plain')),
  digest TEXT NOT NULL CHECK (length(digest) = 64),
  size INTEGER NOT NULL CHECK (size >= 0 AND size <= 4194304),
  locator TEXT NOT NULL,
  PRIMARY KEY (artifact_id, revision)
);
CREATE TRIGGER immutable_revision_update BEFORE UPDATE ON revisions
BEGIN SELECT RAISE(ABORT, 'immutable revision'); END;
CREATE TRIGGER immutable_revision_delete BEFORE DELETE ON revisions
BEGIN SELECT RAISE(ABORT, 'immutable revision'); END;
CREATE TRIGGER immutable_artifact_scope BEFORE UPDATE ON artifacts
BEGIN SELECT RAISE(ABORT, 'immutable artifact scope'); END;
CREATE TABLE probe_budget (
  day TEXT PRIMARY KEY,
  requests INTEGER NOT NULL DEFAULT 0,
  response_bytes INTEGER NOT NULL DEFAULT 0
);
