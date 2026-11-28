"""CLI: ``admin-site serve`` runs the ops web UI (sessions + log streaming)."""

import argparse
import logging
import os
from pathlib import Path

from . import __version__
from .advisor_client import AdvisorAdminClient
from .auth import AuthClient
from .server import SERVICES, build_server

LOG = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description='会参谋运维台（admin-site）')
    parser.add_argument('--version', action='version', version=__version__)
    sub = parser.add_subparsers(dest='command', required=True)

    serve = sub.add_parser('serve', help='run the admin site')
    serve.add_argument('--advisor-url', default='http://127.0.0.1:8380')
    serve.add_argument('--logs-dir', type=Path, default=Path('data/local/logs'),
                       help='log directory as written by scripts/local-up.sh '
                            '(default data/local/logs; files: '
                            + ', '.join(SERVICES.values()) + ')')
    serve.add_argument('--auth-url', default='http://127.0.0.1:8381',
                       help='auth node internal URL (default http://127.0.0.1:8381)')
    serve.add_argument('--public-prefix', default='',
                       help='public path prefix (prod behind nginx: /admin)')
    serve.add_argument('--session-ttl-s', type=float, default=28800,
                       help='admin session lifetime seconds (default 28800)')
    serve.add_argument('--host', default='127.0.0.1',
                       help='bind address (default 127.0.0.1)')
    serve.add_argument('--port', type=int, default=8390)
    serve.add_argument('--bootstrap-bytes', type=int, default=65536,
                       help='initial tail bytes per log stream (default 65536)')
    serve.add_argument('--poll-s', type=float, default=0.5,
                       help='log follow poll interval seconds (default 0.5)')

    args = parser.parse_args()
    admin_users = [part for part in os.environ.get('ADMIN_USERS', '').split(',')
                   if part.strip()]
    if not admin_users:
        parser.error('ADMIN_USERS is required (comma-separated Feishu user_id)')
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')

    advisor = AdvisorAdminClient(args.advisor_url)
    server = build_server(advisor, args.logs_dir, admin_users=admin_users,
                          auth_client=AuthClient(args.auth_url),
                          public_prefix=args.public_prefix,
                          session_ttl_s=args.session_ttl_s,
                          host=args.host, port=args.port,
                          bootstrap_bytes=args.bootstrap_bytes, poll_s=args.poll_s)
    LOG.info('admin-site %s on http://%s:%s (advisor=%s, logs-dir=%s, users=%d)',
             __version__, args.host, args.port, args.advisor_url, args.logs_dir,
             len(admin_users))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
