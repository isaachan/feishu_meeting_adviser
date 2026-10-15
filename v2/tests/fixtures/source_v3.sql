-- Commit abea6c79e306aa563c5d590e70ae4f88ff1c46ee.
CREATE TABLE meetings (meeting_id TEXT PRIMARY KEY, checkpoint TEXT NOT NULL, state TEXT);
CREATE TABLE captions (meeting_id TEXT NOT NULL REFERENCES meetings(meeting_id), sentence_key TEXT NOT NULL, fingerprint TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY (meeting_id,sentence_key));
CREATE TABLE deliveries (sequence INTEGER PRIMARY KEY AUTOINCREMENT, meeting_id TEXT NOT NULL REFERENCES meetings(meeting_id), delivery_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, body TEXT NOT NULL, created_ns INTEGER NOT NULL);
CREATE INDEX deliveries_meeting ON deliveries(meeting_id,sequence);
CREATE TABLE consumers (meeting_id TEXT NOT NULL REFERENCES meetings(meeting_id), consumer_id TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0, offered INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (meeting_id,consumer_id));
CREATE TABLE attempts (meeting_id TEXT NOT NULL, consumer_id TEXT NOT NULL, sequence INTEGER NOT NULL REFERENCES deliveries(sequence) ON DELETE CASCADE, count INTEGER NOT NULL, PRIMARY KEY (meeting_id,consumer_id,sequence));
CREATE TABLE risks (meeting_id TEXT NOT NULL REFERENCES meetings(meeting_id), identity TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY (meeting_id,identity));
CREATE TABLE sessions (app_key TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE conflicts (meeting_id TEXT NOT NULL, sentence_key TEXT NOT NULL, delivery_id TEXT PRIMARY KEY REFERENCES deliveries(delivery_id), resolved INTEGER NOT NULL DEFAULT 0);
INSERT INTO meetings(meeting_id,checkpoint) VALUES ('synthetic-meeting','{}');
INSERT INTO sessions(app_key,body) VALUES ('synthetic-app','{"meeting_id":"synthetic-meeting","phase":"joined"}');
PRAGMA user_version=3;
