"""CLI entry: ``auth serve --data-dir … --base-url …``."""

import argparse
import json
import logging
from pathlib import Path

from . import __version__
from .api import build_server
from .config import load_credentials, load_config
from .service import AuthService

LOG = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(description='会参谋 OAuth 鉴权服务（auth 节点）')
    parser.add_argument('--version', action='version', version=__version__)
    sub = parser.add_subparsers(dest='command', required=True)

    check = sub.add_parser('check', help='validate configuration and exit')
    check.add_argument('--data-dir', type=Path, default=Path('data'))

    serve = sub.add_parser('serve', help='run the auth service')
    serve.add_argument('--data-dir', type=Path, default=Path('data'))
    serve.add_argument('--port', type=int, default=8381)
    serve.add_argument('--base-url', required=True,
                       help='externally reachable base, e.g. https://auth.example.internal')
    serve.add_argument('--state-ttl', type=int, default=600)
    serve.add_argument('--host', default='127.0.0.1')

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')

    if args.command == 'check':
        credentials = load_credentials(args.data_dir)
        print(json.dumps({'status': 'ok', 'app_id': credentials.app_id,
                          'data_dir': str(args.data_dir)}))
        return

    config = load_config(args.data_dir, port=args.port, base_url=args.base_url,
                         state_ttl_s=args.state_ttl)
    service = AuthService(config.credentials, config.data_dir, base_url=config.base_url,
                          state_ttl_s=config.state_ttl_s)
    server = build_server(service, host=args.host, port=args.port)
    LOG.info('auth %s listening on %s (redirect %s)', __version__,
             server.server_address, service.redirect_uri)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
