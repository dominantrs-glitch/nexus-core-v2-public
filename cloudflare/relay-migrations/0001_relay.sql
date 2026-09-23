CREATE TABLE relay_budget(day TEXT PRIMARY KEY, requests INTEGER NOT NULL, bytes INTEGER NOT NULL, auth INTEGER NOT NULL);
CREATE TABLE relay_flows(id TEXT PRIMARY KEY, cookie_hash TEXT NOT NULL, phase TEXT NOT NULL,
  payload TEXT NOT NULL, expires INTEGER NOT NULL);
CREATE TABLE relay_grants(user_id TEXT NOT NULL, client_id TEXT NOT NULL,
  epoch INTEGER NOT NULL DEFAULT 1, enabled INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY(user_id,client_id));
CREATE TABLE relay_used_codes(digest TEXT PRIMARY KEY, expires INTEGER NOT NULL);
