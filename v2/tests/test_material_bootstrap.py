from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest

from feishu_meeting_adviser.material_bootstrap import MaterialBootstrapper
from feishu_meeting_adviser.materials import MaterialError


class FakeData:
    def __init__(self):
        self.sessions = []
        self.states = []
        self.loading = []

    def ensure_session(self, session):
        self.sessions.append(session)

    def set_materials(self, meeting_id, snapshot, *, now):
        self.states.append((meeting_id, snapshot, now))

    def mark_material_loading(self, meeting_id):
        self.loading.append(meeting_id)


class FakePreview:
    records = []

    def __init__(self, *_, **__):
        self.records = []
        FakePreview.records = self.records

    def write(self, record):
        self.records.append(record)


class MaterialBootstrapperTests(unittest.TestCase):
    def config(self, *, enabled):
        context = SimpleNamespace(
            enabled=enabled,
            preview_bytes=1000,
            preview_backups=1,
            calendar_id='calendar',
            event_id='event',
        )
        return SimpleNamespace(
            meeting_context=context,
            data_directory=Path('/synthetic'),
            credentials=object(),
        )

    def bootstrapper(self, data, *, enabled, loader):
        guards = []
        bootstrapper = MaterialBootstrapper(
            data,
            self.config(enabled=enabled),
            {'meeting_id': 'm1', 'joined_at': 1},
            client=None,
            loader=loader,
            preview_display=None,
            stop_event=threading.Event(),
            clock=lambda: 2,
            maintenance_lock=nullcontext,
            guard=lambda: guards.append(True),
            preview_factory=FakePreview,
        )
        return bootstrapper, guards

    def test_disabled_context_persists_once_without_calling_loader(self):
        data = FakeData()
        calls = []
        bootstrapper, guards = self.bootstrapper(
            data, enabled=False, loader=lambda *_args, **_kwargs: calls.append(True))
        bootstrapper.ensure()
        bootstrapper.ensure()
        self.assertEqual(calls, [])
        self.assertEqual(len(data.sessions), 1)
        self.assertEqual(data.states[0][1]['state'], 'disabled')
        self.assertEqual(guards, [True])

    def test_enabled_context_transitions_loading_to_ready(self):
        data = FakeData()
        snapshot = {
            'state': 'ready',
            'context_version': 'v1',
            'documents': [],
            'subject_hash': None,
        }
        bootstrapper, guards = self.bootstrapper(
            data, enabled=True, loader=lambda *_args, **_kwargs: snapshot)
        bootstrapper.ensure()
        self.assertEqual(data.loading, ['m1'])
        self.assertEqual(data.states, [('m1', snapshot, 2)])
        self.assertEqual(len(guards), 2)
        self.assertTrue(bootstrapper.loaded)

    def test_loader_failure_persists_unavailable_before_raising(self):
        data = FakeData()

        def fail(*_args, **_kwargs):
            raise ValueError('private material detail')

        bootstrapper, _ = self.bootstrapper(data, enabled=True, loader=fail)
        with self.assertRaisesRegex(MaterialError, 'material_load_failed'):
            bootstrapper.ensure()
        self.assertEqual(data.states[-1][1]['state'], 'unavailable')
        self.assertEqual(data.states[-1][1]['reason'], 'material_load_failed')
        self.assertNotIn('private material detail', str(FakePreview.records))


if __name__ == '__main__':
    unittest.main()
