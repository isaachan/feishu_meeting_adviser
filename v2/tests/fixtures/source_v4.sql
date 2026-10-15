-- Transitional v4 shape accepted by the v5 migration; no released commit used it.
CREATE TABLE meetings (meeting_id TEXT PRIMARY KEY, checkpoint TEXT NOT NULL, state TEXT);
CREATE TABLE captions (meeting_id TEXT NOT NULL REFERENCES meetings(meeting_id), sentence_key TEXT NOT NULL, fingerprint TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY (meeting_id,sentence_key));
CREATE TABLE deliveries (sequence INTEGER PRIMARY KEY AUTOINCREMENT, meeting_id TEXT NOT NULL REFERENCES meetings(meeting_id), delivery_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, body TEXT NOT NULL, created_ns INTEGER NOT NULL);
CREATE INDEX deliveries_meeting ON deliveries(meeting_id,sequence);
CREATE TABLE consumers (meeting_id TEXT NOT NULL REFERENCES meetings(meeting_id), consumer_id TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0, offered INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (meeting_id,consumer_id));
CREATE TABLE attempts (meeting_id TEXT NOT NULL, consumer_id TEXT NOT NULL, sequence INTEGER NOT NULL REFERENCES deliveries(sequence) ON DELETE CASCADE, count INTEGER NOT NULL, PRIMARY KEY (meeting_id,consumer_id,sequence));
CREATE TABLE risks (meeting_id TEXT NOT NULL REFERENCES meetings(meeting_id), identity TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY (meeting_id,identity));
CREATE TABLE sessions (app_key TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE conflicts (meeting_id TEXT NOT NULL, sentence_key TEXT NOT NULL, delivery_id TEXT PRIMARY KEY REFERENCES deliveries(delivery_id), resolved INTEGER NOT NULL DEFAULT 0);
CREATE TABLE memberships (meeting_id TEXT PRIMARY KEY, active INTEGER NOT NULL, left_at TEXT);
CREATE TABLE tombstones (identity TEXT PRIMARY KEY, deleted_at TEXT NOT NULL);
INSERT INTO meetings(meeting_id,checkpoint) VALUES ('synthetic-meeting','{}');
PRAGMA user_version=4;
