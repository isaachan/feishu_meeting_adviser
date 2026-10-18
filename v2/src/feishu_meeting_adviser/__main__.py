"""Explicit local CLI. Credentials and meeting number are read only from YAML."""
import argparse
from dataclasses import replace
from pathlib import Path
import sqlite3
import sys

from . import __version__
from .client import ApiError, FeishuClient, StopRequested
from .config import ConfigError, DEFAULT_CONFIG, load_config
from .infrastructure.errors import LifecycleError
from .llm import ModelError
from .storage import StorageError
from .adapters.persistence.factory import StorageFactory
from .cli.commands import COMMAND_HANDLERS, CommandContext
from .cli.bootstrap import storage_plan


def parser():
    root = argparse.ArgumentParser(description='Durable Feishu meeting transcript source')
    root.add_argument('--version', action='version', version=__version__)
    root.add_argument('--config', type=Path, default=DEFAULT_CONFIG, help='private local YAML configuration')
    commands = root.add_subparsers(dest='command')
    commands.add_parser('join', help='explicitly join the configured meeting')
    leave = commands.add_parser('leave', help='explicitly leave the saved bot meeting')
    leave.add_argument('--retry-uncertain', action='store_true', help='retry only after checking meeting UI')
    collect = commands.add_parser('collect', help='collect continuously; Ctrl-C stops polling only')
    collect.add_argument('--once', action='store_true', help='one successful page-budget cycle (may leave pending pages)')
    collect.add_argument('--no-text', dest='show_text', action='store_false', default=True,
                         help='do not print caption text, speakers or timestamps')
    status = commands.add_parser('status', help='show safe session and backlog statistics')
    status.add_argument('--consumer')
    consume = commands.add_parser('consume', help='consume with the configured model backend or the fixed test rule')
    consume.add_argument('--consumer')
    consume.add_argument('--limit', type=int, default=100)
    consume.add_argument('--backend', choices=('codex','opencode','rule'), help='override llm.backend for this invocation')
    consume.add_argument('--watch', action='store_true', help='continuously consume and decide; Ctrl-C stops the consumer only')
    reset = commands.add_parser('reset-session', help='reset stale/uncertain session after manually verifying bot absence')
    reset.add_argument('--confirmed-bot-left', action='store_true', required=True)
    resolve = commands.add_parser('resolve-conflict', help='explicitly accept a stored candidate after local review')
    resolve.add_argument('--delivery-id', required=True)
    commands.add_parser('prune', help='remove acknowledged expired records; stop collector first')
    delete = commands.add_parser('delete-meeting-data', help='irreversibly delete saved meeting content and consumer copies')
    delete.add_argument('--confirm', action='store_true', required=True)
    inspect = commands.add_parser('conflicts', help='list unresolved candidate hashes; text requires explicit flag')
    inspect.add_argument('--show-text', action='store_true')
    replies = commands.add_parser('replies', help='inspect saved model reply states; text requires explicit flag')
    replies.add_argument('--show-text', action='store_true')
    resolve_reply = commands.add_parser('resolve-reply', help='resolve one uncertain/rejected model send after checking meeting UI; stop consume first')
    resolve_reply.add_argument('--uuid', required=True)
    resolve_reply.add_argument('--outcome', choices=('sent','retry','cancel'), required=True)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    if not args.command:
        parser().print_help()
        return 0
    store = None
    try:
        config = load_config(args.config)
        if args.command == 'consume' and args.backend:
            config = replace(config,llm=replace(config.llm,backend=args.backend))
        if args.command in ('status','consume'):
            args.consumer = args.consumer or ('adviser' if config.llm.backend in ('codex','opencode') else 'fake-qa')
        storage_factory = StorageFactory(config)
        plan = storage_plan(args.command)
        if plan.source_readonly and not storage_factory.source_path.exists():
            if args.command == 'status':
                print('{"session_phase": "none"}')
                return 0
            raise StorageError('no saved product database for read-only command')
        if not plan.source_readonly:
            storage_factory.initialize_source()
        store = storage_factory.source(readonly=plan.source_readonly)
        context = CommandContext(
            config, storage_factory, plan, store, FeishuClient)
        return COMMAND_HANDLERS[args.command](context, args)
    except (ConfigError, StorageError, LifecycleError, ApiError, ModelError) as exc:
        print('Operation stopped: ' + str(exc), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error):
        print('Operation stopped: local file/database error; check private storage and free space.', file=sys.stderr)
        return 1
    except (KeyboardInterrupt, StopRequested):
        print('Operation interrupted; check saved session before retrying a meeting operation.', file=sys.stderr)
        return 130
    finally:
        if store:
            store.close()


if __name__ == '__main__':
    raise SystemExit(main())
