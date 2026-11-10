"""CLI: ``advisor serve`` wires store + ingest + supervisor + HTTP API."""

import argparse
import logging
from pathlib import Path

from . import __version__
from .api import AdvisorApi, StubCalendarValidator, build_api_server
from .auth_client import HttpAuthClient
from .ingest import IngestServer, start_ingest_thread
from .model_backend import RemoteModelBackend
from .rule_backend import RuleBackend
from .settings import AdviserSettings
from .store import SessionStore
from .stt_client import RemoteSttSessionFactory
from .supervisor import SessionSupervisor

LOG = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description='会参谋云端会话核心（advisor 节点）')
    parser.add_argument('--version', action='version', version=__version__)
    sub = parser.add_subparsers(dest='command', required=True)

    init = sub.add_parser('init', help='initialize the session database')
    init.add_argument('--data-dir', type=Path, default=Path('data'))

    serve = sub.add_parser('serve', help='run the advisor node')
    serve.add_argument('--data-dir', type=Path, default=Path('data'))
    serve.add_argument('--api-port', type=int, default=8380)
    serve.add_argument('--ingest-port', type=int, default=8400)
    serve.add_argument('--ingest-url', default='ws://127.0.0.1:8400/ingest',
                       help='ingest URL rendered into recorder commands')
    serve.add_argument('--stt-url', default='ws://127.0.0.1:8392')
    serve.add_argument('--gateway-url', default='http://127.0.0.1:8377')
    serve.add_argument('--auth-url', default='http://127.0.0.1:8381')
    serve.add_argument('--model', choices=['remote', 'rule'], default='remote')
    serve.add_argument('--remote-backend', choices=['fake', 'opencode'], default='opencode',
                       help='gateway backend tag sent with inference jobs')
    serve.add_argument('--calendar-stub', action='store_true',
                       help='stub calendar validation (stub-first stages only)')
    serve.add_argument('--calendar-stub-description', default='')

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    if args.command == 'init':
        SessionStore.initialize(args.data_dir / 'adviser.sqlite3')
        print('initialized', args.data_dir / 'adviser.sqlite3')
        return

    database = args.data_dir / 'adviser.sqlite3'
    if not database.exists():
        SessionStore.initialize(database)
    settings = AdviserSettings(remote_url=args.gateway_url,
                               remote_backend=args.remote_backend)
    store = SessionStore(database)

    if args.model == 'rule':
        backend_factory = lambda: RuleBackend()  # noqa: E731
    else:
        backend_factory = lambda: RemoteModelBackend(settings)  # noqa: E731

    ingest = IngestServer(store, RemoteSttSessionFactory(args.stt_url),
                          port=args.ingest_port)
    supervisor = SessionSupervisor(store, settings, backend_factory=backend_factory,
                                   ingest_activity=ingest.activity_snapshot,
                                   close_ingest=ingest.close_session)
    api = AdvisorApi(store, supervisor, auth_client=HttpAuthClient(args.auth_url),
                     calendar_validator=(StubCalendarValidator(args.calendar_stub_description)
                                         if args.calendar_stub else _real_calendar()),
                     ingest_url=args.ingest_url)
    stop_ingest = start_ingest_thread(ingest)
    supervisor.start()
    server = build_api_server(api, port=args.api_port)
    LOG.info('advisor %s: api on %s, ingest on %s (model=%s)',
             __version__, server.server_address, args.ingest_port, args.model)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        supervisor.stop()
        stop_ingest()
        store.close()


def _real_calendar():
    raise SystemExit('real calendar validation lands with stage 5 (M3); '
                     'use --calendar-stub for stub-first stages')


if __name__ == '__main__':
    main()
