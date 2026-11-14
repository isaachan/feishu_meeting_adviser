#!/usr/bin/env python3
"""Stop the production stack without silently interrupting active meetings."""
import argparse
import json
import subprocess
import sys
import time
from urllib import request

SERVICES = ('admin-site', 'bot', 'advisor', 'llm-gateway', 'stt-server', 'auth')
BASE = 'http://127.0.0.1:8380'
opener = request.build_opener(request.ProxyHandler({}))


def api(path, method='GET'):
    req = request.Request(BASE + path, data=b'' if method == 'POST' else None, method=method)
    with opener.open(req, timeout=30) as response:
        return json.load(response)


def active():
    return [s for s in api('/admin/sessions')['sessions']
            if s['phase'] in ('recording', 'draining')]


def systemctl(*args, check=True):
    return subprocess.run(['sudo', 'systemctl', *args], check=check)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--drain', action='store_true', help='end active meetings and wait')
    parser.add_argument('--timeout', type=int, default=300)
    args = parser.parse_args()
    running = subprocess.run(['systemctl', 'is-active', '--quiet', 'meeting-advisor@advisor.service']).returncode == 0
    if running:
        sessions = active()  # Fail closed if advisor cannot be queried.
        if sessions and not args.drain:
            raise RuntimeError(f'{len(sessions)} active/draining sessions; retry with --drain')
        if args.drain:
            # Prevent new bot-initiated meetings during drain; restart on failure.
            systemctl('stop', 'meeting-advisor@bot.service')
            try:
                for session in sessions:
                    api('/admin/sessions/' + session['session_id'] + '/end', 'POST')
                deadline = time.monotonic() + args.timeout
                while active():
                    if time.monotonic() >= deadline:
                        raise RuntimeError('drain timed out; services remain running')
                    time.sleep(2)
            except Exception:
                systemctl('start', 'meeting-advisor@bot.service', check=False)
                raise
    for name in SERVICES:
        systemctl('stop', f'meeting-advisor@{name}.service')
    print('Production services stopped')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Production stop failed: {exc}', file=sys.stderr)
        sys.exit(1)
