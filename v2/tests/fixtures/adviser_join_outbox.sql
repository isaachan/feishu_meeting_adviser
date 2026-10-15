-- Generated from commit 26dc3f43cc66a69a8f5c142e83b8d8edf0243672.
CREATE TABLE meetings (meeting_id TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0, decided_generation INTEGER NOT NULL DEFAULT 0, participation TEXT NOT NULL DEFAULT 'active', source_state TEXT, error TEXT, retry_at REAL NOT NULL DEFAULT 0, cooldown_until REAL NOT NULL DEFAULT 0, join_instance TEXT, role_version TEXT, source_status TEXT);
CREATE TABLE inputs (delivery_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, sequence INTEGER NOT NULL, body TEXT NOT NULL, created_ns INTEGER NOT NULL);
CREATE TABLE context (meeting_id TEXT NOT NULL, sentence_key TEXT NOT NULL, body TEXT NOT NULL, suppressed INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL, PRIMARY KEY (meeting_id,sentence_key));
CREATE TABLE jobs (job_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, generation INTEGER NOT NULL, snapshot TEXT NOT NULL, role_version TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', result TEXT, created_ns INTEGER NOT NULL);
CREATE INDEX input_meeting ON inputs(meeting_id,sequence);
CREATE INDEX job_meeting ON jobs(meeting_id,generation);
CREATE TABLE outbox (uuid TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL, evidence TEXT NOT NULL, generation INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending', error TEXT, created_ns INTEGER NOT NULL, sent_at REAL);
INSERT INTO meetings(meeting_id,generation,join_instance) VALUES ('synthetic-meeting',1,'synthetic-join');
