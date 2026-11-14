"""Gateway CLI: serve the inference contract on localhost."""
import argparse
import json
import logging
from pathlib import Path
import signal
import sys
import threading
import time

from . import __version__
from .api import GatewayApp, create_server
from .backends.fake import FakeBackend
from .scheduler import ExecutionScheduler
from .service import GatewayService, Limits
from .store import GatewayStore

LOG = logging.getLogger(__name__)

DEFAULT_PORT = 8377


def parser():
    root = argparse.ArgumentParser(description='Local LLM inference gateway')
    root.add_argument('--version', action='version', version=__version__)
    commands = root.add_subparsers(dest='command')
    serve = commands.add_parser('serve', help='serve the inference contract')
    serve.add_argument('--host', default='127.0.0.1', help='bind address (default 127.0.0.1)')
    serve.add_argument('--port', type=int, default=DEFAULT_PORT)
    serve.add_argument('--database', type=Path, default=None,
                       help='task database (default: <repo>/data/llm-gateway/tasks.sqlite3)')
    serve.add_argument('--workers', type=int, default=4, help='global execution concurrency')
    serve.add_argument('--queue', type=int, default=64, help='queued task limit')
    serve.add_argument('--retention-hours', type=float, default=24.0)
    serve.add_argument('--opencode-executable', default='opencode')
    serve.add_argument('--opencode-model', default='zai-coding-plan/glm-5.3')
    serve.add_argument('--opencode-auth', type=Path, default=None,
                       help='opencode auth.json providing the model provider key')
    serve.add_argument('--skills-dir', type=Path, default=None,
                       help='OpenCode skill directory (default: packaged skills)')
    serve.add_argument('--auth-url', default='http://127.0.0.1:8381',
                       help='internal auth service URL for read-only tools')
    serve.add_argument('--noam-bin', default='noam', help='noam CLI executable for NSDP lookup')
    serve.add_argument('--runtime-dir', type=Path, default=None,
                       help='base directory for isolated backend runtimes')
    return root


def default_database() -> Path:
    return Path(__file__).resolve().parents[2].parent / 'data' / 'llm-gateway' / 'tasks.sqlite3'


def backend_factory(options):
    def factory(backend: str):
        if backend == 'fake':
            return FakeBackend()
        if backend == 'opencode':
            from .backends.opencode import OpenCodeBackend
            return OpenCodeBackend(
                executable=options.opencode_executable,
                model=options.opencode_model,
                auth_path=options.opencode_auth,
                runtime_base=options.runtime_dir,
                skills_dir=options.skills_dir, auth_url=options.auth_url,
                noam_bin=options.noam_bin)
        raise ValueError('unsupported backend')
    return factory


def main(argv=None):
    args = parser().parse_args(argv)
    if not args.command:
        parser().print_help()
        return 0
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    try:
        database = args.database or default_database()
        store = GatewayStore(database)
        recovered = store.recover_interrupted()
        from .catalog import MeetingCatalogs
        from .nsdp_query import NsdpQuery
        from .nsdp_tool_server import credential_provider
        catalogs = MeetingCatalogs(database.with_name(database.name + '.catalog.sqlite3'),
                                   NsdpQuery(credential_provider(args.auth_url),
                                             noam_bin=args.noam_bin, timeout_s=120))
        service = GatewayService(store, limits=Limits(queue=args.queue), catalogs=catalogs)
        scheduler = ExecutionScheduler(store, backend_factory(args), workers=args.workers)
        scheduler.start()
        server = create_server(GatewayApp(service, scheduler),
                               host=args.host, port=args.port)
        expiry_stop = threading.Event()

        def sweep():
            while not expiry_stop.wait(600):
                store.expire(retention_hours=args.retention_hours)
                catalogs.expire(retention_hours=args.retention_hours)

        sweeper = threading.Thread(target=sweep, name='gateway-expiry', daemon=True)
        sweeper.start()
        LOG.info('llm-gateway %s listening on %s:%s (database %s recovered %s '
                 'workers %s pid %s)', __version__, args.host,
                 server.server_address[1], database, recovered, args.workers,
                 __import__('os').getpid())

        def stop(signum, frame):
            threading.Thread(target=server.shutdown, daemon=True).start()

        previous = signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)
        try:
            server.serve_forever(poll_interval=0.1)
        finally:
            signal.signal(signal.SIGINT, previous)
            expiry_stop.set()
            sweeper.join(timeout=2)
            scheduler.stop()
            server.server_close()
            catalogs.close()
            store.close()
    except (OSError, ValueError) as exc:
        print('Operation stopped: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
