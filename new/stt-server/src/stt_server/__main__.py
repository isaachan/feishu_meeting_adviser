"""stt-server CLI: serve the streaming STT WebSocket service."""
import argparse
import asyncio
import json
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .service import MAX_MESSAGE_BYTES, SttService


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description='Streaming STT server')
    root.add_argument('--version', action='version', version=__version__)
    commands = root.add_subparsers(dest='command')
    serve = commands.add_parser('serve', help='run the WebSocket service')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=8392)
    serve.add_argument('--health-port', type=int, default=8393,
                       help='HTTP healthz port (0 disables)')
    serve.add_argument('--model-dir', type=Path, required=True)
    serve.add_argument('--threads', type=int, default=2,
                       help='inference threads per session')
    serve.add_argument('--max-sessions', type=int, default=8)
    serve.add_argument('--idle-timeout', type=float, default=60.0)
    serve.add_argument('--auth', choices=('none', 'feishu'), default='none',
                       help='session authorization mode (default: none, internal baseline)')
    serve.add_argument('--auth-window-seconds', type=int, default=1800,
                       help='meeting time window slack at both ends (default: 1800)')
    serve.add_argument('--auth-cache-seconds', type=float, default=600.0,
                       help='cache TTL for passed validations (default: 600)')
    return root


def _health_server(service: SttService, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            if self.path == '/stats':
                body = json.dumps(service.stats(), ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if self.path != '/healthz':
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps({'status': 'ok', 'model': service.model_name,
                               'sessions': service.state.active,
                               'auth': 'feishu' if service.auth is not None else 'none'}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002, N802 - silence
            _ = format, args

    return ThreadingHTTPServer(('0.0.0.0', port), Handler)


async def _run(args) -> None:
    import websockets

    service = SttService(model_dir=args.model_dir, num_threads=args.threads,
                         max_sessions=args.max_sessions,
                         idle_timeout=args.idle_timeout,
                         auth=_build_auth(args))
    health = None
    if args.health_port:
        health = _health_server(service, args.health_port)
        threading.Thread(target=health.serve_forever, daemon=True,
                         name='stt-health').start()

    async with websockets.serve(service.handle, args.host, args.port,
                                max_size=MAX_MESSAGE_BYTES,
                                ping_interval=20, ping_timeout=20):
        print(json.dumps({'listening': f'{args.host}:{args.port}',
                          'health_port': args.health_port,
                          'auth': args.auth,
                          'model_dir': str(args.model_dir)}), flush=True)
        await asyncio.Event().wait()  # until cancelled


def _build_auth(args):
    if args.auth != 'feishu':
        return None
    from .auth import AuthValidator

    return AuthValidator(window_seconds=args.auth_window_seconds,
                         cache_seconds=args.auth_cache_seconds)


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    if not args.command:
        parser().print_help()
        return 0
    try:
        asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        print(f'Operation stopped: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
