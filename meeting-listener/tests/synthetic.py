"""Local synthetic fixtures; no dependency on another module's test suite."""
import json

MID = '987654321012345678'


def event(sid='s1', text='synthetic', timestamp='2026-10-13T00:00:00.100Z', *, nested=False, **item):
    payload = {'transcript_received_items': [{'sentence_id': sid, 'text': text, **item}]}
    if nested:
        payload['activity_event_type'] = 'transcript_received'
        payload = json.dumps(payload)
    return {'event_type': 'meeting.activity' if nested else 'transcript_received',
            'event_id': 'synthetic-event-' + str(sid), 'event_time': timestamp, 'payload': payload}


def page(events=(), more=False, cursor=None):
    return {'data': {'events': list(events), 'has_more': more, 'page_token': cursor}}


class FakeAPI:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.queries = []

    def events(self, query):
        self.queries.append(dict(query))
        return next(self.pages)


class BotAPI:
    def __init__(self):
        self.joins = self.leaves = self.event_calls = 0
        self.passwords = []
        self.failure = None
        self.event_failure = None

    def join(self, meeting_no, password):
        self.joins += 1
        self.passwords.append(password)
        if self.failure:
            raise self.failure
        return {'data': {'meeting': {'id': MID, 'meeting_no': meeting_no},
                         'join_user': {'id': 'synthetic-bot'}}}

    def leave(self, meeting_id):
        self.leaves += 1
        if self.failure:
            raise self.failure
        return {'data': {}}

    def events(self, query):
        self.event_calls += 1
        if self.event_failure:
            raise self.event_failure
        return page([event()])
