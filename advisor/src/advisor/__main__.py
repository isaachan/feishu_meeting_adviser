"""CLI: ``advisor serve`` wires store + ingest + supervisor + HTTP API."""

import argparse
import logging
import signal
import threading
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
    serve.add_argument('--api-host', default='127.0.0.1',
                       help='API bind address (default 127.0.0.1)')
    serve.add_argument('--api-port', type=int, default=8380)
    serve.add_argument('--ingest-port', type=int, default=8400)
    serve.add_argument('--ingest-url', default='ws://127.0.0.1:8400/ingest',
                       help='ingest URL rendered into recorder commands')
    serve.add_argument('--stt-url', default='ws://127.0.0.1:8392')
    serve.add_argument('--gateway-url', default='http://127.0.0.1:8377')
    serve.add_argument('--auth-url', default='http://127.0.0.1:8381')
    serve.add_argument('--observe-poll-s', type=float, default=15)
    serve.add_argument('--observe-timeout-s', type=float, default=10)
    serve.add_argument('--max-observation-age-s', type=float, default=60)
    serve.add_argument('--approval-ttl-s', type=float, default=1800)
    serve.add_argument('--model', choices=['remote', 'rule'], default='remote')
    serve.add_argument('--remote-backend', choices=['fake', 'opencode'], default='opencode',
                       help='gateway backend tag sent with inference jobs')
    serve.add_argument('--calendar', choices=['stub', 'feishu'], default='feishu',
                       help='calendar validation mode (default: real Feishu reads)')
    serve.add_argument('--calendar-stub', action='store_true',
                       help='[deprecated] same as --calendar stub')
    serve.add_argument('--calendar-stub-description', default='')
    serve.add_argument('--nsdp', choices=['off', 'noam', 'stub'], default='noam',
                       help='NSDP organizer token gate/fetch mode (default: noam CLI)')
    serve.add_argument('--noam-bin', default='noam', help='noam CLI path for --nsdp noam')
    serve.add_argument('--nsdp-timeout-s', type=float, default=60,
                       help='per-call noam timeout for --nsdp noam (default 60; '
                            'nsdp info walks the whole catalog and can take 30s+)')
    serve.add_argument('--nsdp-join-budget-s', type=float, default=12,
                       help='noam budget during join; on expiry the join degrades '
                            'to no-NSDP-material and the supervisor backfills '
                            '(default 12)')
    serve.add_argument('--nsdp-refresh-s', type=float, default=900,
                       help='NSDP material refresh interval seconds')
    serve.add_argument('--nsdp-retry-s', type=float, default=60,
                       help='NSDP re-auth retry interval seconds (session on hold)')
    serve.add_argument('--nsdp-nag-s', type=float, default=300,
                       help='NSDP re-auth DM reminder interval seconds')

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    if args.command == 'init':
        SessionStore.initialize(args.data_dir / 'adviser.sqlite3')
        print('initialized', args.data_dir / 'adviser.sqlite3')
        return

    database = args.data_dir / 'adviser.sqlite3'
    if not database.exists():
        SessionStore.initialize(database)
    if min(args.observe_poll_s, args.observe_timeout_s, args.max_observation_age_s,
           args.approval_ttl_s) <= 0:
        parser.error('meeting observation budgets must be positive')
    settings = AdviserSettings(remote_url=args.gateway_url,
                                remote_backend=args.remote_backend,
                                observe_poll_s=args.observe_poll_s,
                                observe_timeout_s=args.observe_timeout_s,
                                max_observation_age_s=args.max_observation_age_s,
                                approval_ttl_s=args.approval_ttl_s,
                               nsdp_refresh_seconds=args.nsdp_refresh_s,
                               nsdp_retry_seconds=args.nsdp_retry_s,
                               nsdp_nag_seconds=args.nsdp_nag_s)
    store = SessionStore(database)

    nsdp_service = None
    if args.nsdp == 'noam':
        # Only the authorization notification path remains in advisor. The
        # gateway's scoped tool performs actual NSDP lookups on demand.
        from .nsdp import NsdpService, StubNsdpClient
        nsdp_service = NsdpService(HttpAuthClient(args.auth_url),
                                   StubNsdpClient(), store)
    elif args.nsdp == 'stub':
        from .nsdp import NsdpService, StubNsdpClient
        nsdp_service = NsdpService(HttpAuthClient(args.auth_url), StubNsdpClient(), store)

    if args.model == 'rule':
        backend_factory = lambda: RuleBackend()  # noqa: E731
    else:
        backend_factory = lambda: RemoteModelBackend(settings)  # noqa: E731

    ingest = IngestServer(store, RemoteSttSessionFactory(args.stt_url),
                          port=args.ingest_port)
    from .nsdp import NsdpService, StubNsdpClient
    jira_notifier = nsdp_service or NsdpService(HttpAuthClient(args.auth_url), StubNsdpClient(), store)
    auth_client = HttpAuthClient(args.auth_url)
    meeting_observer = None
    if args.calendar == 'feishu' and not args.calendar_stub:
        from .meeting_observer import FeishuMeetingObserver
        meeting_observer = FeishuMeetingObserver(
            store, auth_client, timeout_s=args.observe_timeout_s,
            max_age_s=args.max_observation_age_s)
    supervisor = SessionSupervisor(store, settings, backend_factory=backend_factory,
                                   ingest_activity=ingest.activity_snapshot,
                                   close_ingest=ingest.close_session,
                                   source_connected=ingest.source_connected,
                                   nsdp=nsdp_service, jira_notifier=jira_notifier,
                                   meeting_observer=meeting_observer)
    if args.calendar == 'stub' or args.calendar_stub:
        calendar_validator = StubCalendarValidator(args.calendar_stub_description)
    else:
        from .calendar import FeishuCalendarValidator
        calendar_validator = FeishuCalendarValidator(HttpAuthClient(args.auth_url))
    api = AdvisorApi(store, supervisor, auth_client=auth_client,
                      calendar_validator=calendar_validator,
                      ingest_url=args.ingest_url, nsdp=nsdp_service,
                      approval_ttl_s=settings.approval_ttl_s,
                     nsdp_join_budget_s=args.nsdp_join_budget_s)
    stop_ingest = start_ingest_thread(ingest)
    supervisor.start()
    server = build_api_server(api, host=args.api_host, port=args.api_port)
    LOG.info('advisor %s: api on %s, ingest on %s (model=%s)',
             __version__, server.server_address, args.ingest_port, args.model)
    def stop(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        supervisor.stop()
        stop_ingest()
        store.close()


if __name__ == '__main__':
    main()
