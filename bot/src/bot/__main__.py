"""CLI: ``bot serve`` wires the service against advisor + Feishu long connection."""

import argparse
import logging
import os

from . import __version__
from .advisor_client import AdvisorClient
from .service import BotService

LOG = logging.getLogger(__name__)


def _env_flag(name, default=True):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ('1', 'true', 'yes', 'on')


def _jira_project_keys(value):
    """Parse a comma-separated Jira project-key allowlist."""
    return tuple(key.strip() for key in (value or '').split(',') if key.strip())


def main() -> None:
    parser = argparse.ArgumentParser(description='会参谋 bot（IM 控制面/通知面）')
    parser.add_argument('--version', action='version', version=__version__)
    sub = parser.add_subparsers(dest='command', required=True)

    serve = sub.add_parser('serve', help='run the bot node')
    serve.add_argument('--advisor-url', default='http://127.0.0.1:8380')
    serve.add_argument('--app-id', default=os.environ.get('BOT_APP_ID', ''))
    serve.add_argument('--app-secret', default=os.environ.get('BOT_APP_SECRET', ''))
    serve.add_argument('--poll-interval', type=float, default=1.0)
    serve.add_argument('--rich-text', action=argparse.BooleanOptionalAction,
                       default=_env_flag('BOT_RICH_TEXT'),
                       help='render Markdown replies as Feishu rich text '
                            '(default: BOT_RICH_TEXT, on)')
    serve.add_argument('--jira-url', default=os.environ.get('JIRA_URL', ''),
                       help='Jira base URL used to linkify ticket keys '
                            '(default: JIRA_URL)')
    serve.add_argument('--jira-project-keys',
                       default=os.environ.get('JIRA_PROJECT_KEYS', ''),
                       help='comma-separated Jira project-key allowlist for '
                            'linkifying (default: JIRA_PROJECT_KEYS)')

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    if not (args.app_id and args.app_secret):
        raise SystemExit('BOT_APP_ID/BOT_APP_SECRET (or --app-id/--app-secret) required')

    from .lark_bot import run
    service = BotService(AdvisorClient(args.advisor_url), transport=None,
                         poll_interval=args.poll_interval)
    LOG.info('bot %s starting (advisor %s)', __version__, args.advisor_url)
    run(service, args.app_id, args.app_secret, rich_text=args.rich_text,
        jira_url=args.jira_url,
        jira_project_keys=_jira_project_keys(args.jira_project_keys))


if __name__ == '__main__':
    main()
