"""CLI: ``bot serve`` wires the service against advisor + Feishu long connection."""

import argparse
import logging
import os

from . import __version__
from .advisor_client import AdvisorClient
from .service import BotService

LOG = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description='会参谋 bot（IM 控制面/通知面）')
    parser.add_argument('--version', action='version', version=__version__)
    sub = parser.add_subparsers(dest='command', required=True)

    serve = sub.add_parser('serve', help='run the bot node')
    serve.add_argument('--advisor-url', default='http://127.0.0.1:8380')
    serve.add_argument('--app-id', default=os.environ.get('BOT_APP_ID', ''))
    serve.add_argument('--app-secret', default=os.environ.get('BOT_APP_SECRET', ''))
    serve.add_argument('--poll-interval', type=float, default=1.0)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    if not (args.app_id and args.app_secret):
        raise SystemExit('BOT_APP_ID/BOT_APP_SECRET (or --app-id/--app-secret) required')

    from .lark_bot import run
    service = BotService(AdvisorClient(args.advisor_url), transport=None,
                         poll_interval=args.poll_interval)
    LOG.info('bot %s starting (advisor %s)', __version__, args.advisor_url)
    run(service, args.app_id, args.app_secret)


if __name__ == '__main__':
    main()
