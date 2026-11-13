"""Explicit local CLI for advisor meeting sessions.

The legacy Feishu bot pipeline is disabled: sessions are created explicitly and
consumed by their own id. Credentials in the YAML are still required by shared
configuration parsing but no Feishu API call is made for these commands.
"""
import argparse
from dataclasses import replace
from pathlib import Path
import sqlite3
import sys

from . import __version__
from .config import ConfigError, DEFAULT_CONFIG, load_config
from .infrastructure.errors import LifecycleError
from .llm import ModelError
from .storage import StorageError
from .adapters.persistence.factory import StorageFactory
from .cli.commands import COMMAND_HANDLERS, CommandContext
from .cli.bootstrap import storage_plan


def parser():
    root = argparse.ArgumentParser(description='Local meeting adviser sessions')
    root.add_argument('--version', action='version', version=__version__)
    root.add_argument('--config', type=Path, default=DEFAULT_CONFIG, help='private local YAML configuration')
    commands = root.add_subparsers(dest='command')
    new = commands.add_parser('session-new', help='create a local meeting session')
    new.add_argument('--title', default='', help='short human-readable session title')
    new.add_argument('--description-file', help='optional UTF-8 meeting description file')
    commands.add_parser('session-list', help='list local meeting sessions')
    end = commands.add_parser('session-end', help='stop input; ends now when idle, otherwise drains')
    end.add_argument('session')
    end.add_argument('--force', action='store_true',
                     help='end immediately even with pending model work')
    status = commands.add_parser('status', help='show safe session and backlog statistics')
    status.add_argument('session')
    status.add_argument('--consumer')
    consume = commands.add_parser('consume', help='consume one local session with a model backend')
    consume.add_argument('session')
    consume.add_argument('--consumer')
    consume.add_argument('--limit', type=int, default=100)
    consume.add_argument('--backend', choices=('opencode', 'remote'),
                         help='override llm.backend for this invocation')
    consume.add_argument('--watch', action='store_true', help='continuously consume and decide; Ctrl-C stops the consumer only')
    replies = commands.add_parser('replies', help='inspect saved model reply states; text requires explicit flag')
    replies.add_argument('session')
    replies.add_argument('--show-text', action='store_true')
    resolve_reply = commands.add_parser('resolve-reply', help='resolve one uncertain/rejected model send; stop consume first')
    resolve_reply.add_argument('session')
    resolve_reply.add_argument('--uuid', required=True)
    resolve_reply.add_argument('--outcome', choices=('sent', 'retry', 'cancel'), required=True)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    if not args.command:
        parser().print_help()
        return 0
    store = None
    data = None
    try:
        config = load_config(args.config)
        if args.command == 'consume' and args.backend:
            config = replace(config, llm=replace(config.llm, backend=args.backend))
        if args.command in ('status', 'consume'):
            args.consumer = args.consumer or 'adviser'
        storage_factory = StorageFactory(config)
        plan = storage_plan(args.command)
        if plan.source_readonly is False:
            storage_factory.initialize_source()
        if plan.adviser_readonly is False:
            storage_factory.initialize_adviser()
        if plan.source_readonly is False:
            store = storage_factory.source()
        elif plan.source_readonly and storage_factory.source_path.exists():
            store = storage_factory.source(readonly=True)
        elif plan.source_readonly and args.command == 'status':
            print('{"session_phase": "none"}')
            return 0
        elif plan.source_readonly:
            raise StorageError('no saved product database for read-only command')
        if plan.adviser_readonly is not None and (
                plan.adviser_readonly is False or storage_factory.adviser_path.exists()):
            data = storage_factory.adviser(readonly=plan.adviser_readonly)
        context = CommandContext(config, storage_factory, plan, store, data, args)
        return COMMAND_HANDLERS[args.command](context, args)
    except (ConfigError, StorageError, LifecycleError, ModelError) as exc:
        print('Operation stopped: ' + str(exc), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error):
        print('Operation stopped: local file/database error; check private storage and free space.', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Operation interrupted; the session and its inputs stay saved.', file=sys.stderr)
        return 130
    finally:
        if data:
            data.close()
        if store:
            store.close()


if __name__ == '__main__':
    raise SystemExit(main())
