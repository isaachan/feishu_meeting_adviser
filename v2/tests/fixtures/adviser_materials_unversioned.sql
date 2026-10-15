-- Generated from commit dedb86baff901b237dbd9abdfd377b12d20ef206.
CREATE TABLE meetings (meeting_id TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0, decided_generation INTEGER NOT NULL DEFAULT 0, participation TEXT NOT NULL DEFAULT 'active', source_state TEXT, error TEXT, retry_at REAL NOT NULL DEFAULT 0, cooldown_until REAL NOT NULL DEFAULT 0, join_instance TEXT, role_version TEXT, source_status TEXT, material_version TEXT, material_state TEXT);
CREATE TABLE inputs (delivery_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, sequence INTEGER NOT NULL, body TEXT NOT NULL, created_ns INTEGER NOT NULL);
CREATE TABLE context (meeting_id TEXT NOT NULL, sentence_key TEXT NOT NULL, body TEXT NOT NULL, suppressed INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL, PRIMARY KEY (meeting_id,sentence_key));
CREATE TABLE jobs (job_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, generation INTEGER NOT NULL, snapshot TEXT NOT NULL, role_version TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending', result TEXT, created_ns INTEGER NOT NULL, context_version TEXT);
CREATE INDEX input_meeting ON inputs(meeting_id,sequence);
CREATE INDEX job_meeting ON jobs(meeting_id,generation);
CREATE TABLE outbox (uuid TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL, evidence TEXT NOT NULL, generation INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'pending', error TEXT, created_ns INTEGER NOT NULL, sent_at REAL, context_version TEXT);
CREATE TABLE meeting_materials (meeting_id TEXT PRIMARY KEY, body TEXT NOT NULL, loaded_ns INTEGER NOT NULL);
INSERT INTO meetings(meeting_id,generation,join_instance,material_state) VALUES ('synthetic-meeting',1,'synthetic-join','ready');
