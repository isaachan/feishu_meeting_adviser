-- Generated from commit cb22cdcb5efc8c0a09d9568a3859b6694d072448.
CREATE TABLE meetings (meeting_id TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0, decided_generation INTEGER NOT NULL DEFAULT 0, participation TEXT NOT NULL DEFAULT 'active', source_state TEXT, error TEXT, retry_at REAL NOT NULL DEFAULT 0, cooldown_until REAL NOT NULL DEFAULT 0);
CREATE TABLE inputs (delivery_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, sequence INTEGER NOT NULL, body TEXT NOT NULL, created_ns INTEGER NOT NULL);
CREATE TABLE context (meeting_id TEXT NOT NULL, sentence_key TEXT NOT NULL, body TEXT NOT NULL, suppressed INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL, PRIMARY KEY (meeting_id,sentence_key));
CREATE TABLE jobs (job_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, generation INTEGER NOT NULL, snapshot TEXT NOT NULL, role_version TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', result TEXT, created_ns INTEGER NOT NULL);
CREATE INDEX input_meeting ON inputs(meeting_id,sequence);
CREATE INDEX job_meeting ON jobs(meeting_id,generation);
INSERT INTO meetings(meeting_id,generation) VALUES ('synthetic-meeting',1);
