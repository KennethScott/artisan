"""Roast watcher for the Artisan Local Control API.

Prints one compact line every `--every` seconds and returns as soon as something needs a decision:
a new roast event, a lock (override / watchdog / stop), the API going away, or `--dur` elapsed.
Hard safety limit: POSTs /stop itself, without waiting for anyone, if BT exceeds `--limit`.

usage: python watch.py [--dur 60] [--every 15] [--limit 420] [--port 8081]
Token (if configured) from the ARTISAN_API_TOKEN environment variable.
Exit codes: 0 duration elapsed, 10 new event, 11 lock/override, 12 safety stop, 13 API unreachable, 14 not recording.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from typing import Any

TOKEN = os.environ.get('ARTISAN_API_TOKEN', '')


def call(base: str, path: str, body: dict[str, Any] | None = None, timeout: float = 40) -> dict[str, Any]:
    req = urllib.request.Request(base + path, method='POST' if body is not None else 'GET',
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={'Content-Type': 'application/json'})
    if TOKEN:
        req.add_header('Authorization', f'Bearer {TOKEN}')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b'{}')


def mmss(t: float | None) -> str:
    return '--:--' if t is None else f'{int(t) // 60:02d}:{int(t) % 60:02d}'


def line(s: dict[str, Any]) -> str:
    sl = {x['name']: x['value'] for x in s.get('sliders', []) if x.get('visible')}
    p = s.get('pid') or {}
    ref = s.get('bt_background')
    gap = f"{s['bt'] - p['sv']:+.1f}" if s.get('bt') is not None and p.get('active') and p.get('sv') is not None else '-'
    ev = ','.join(k for k, v in (s.get('events') or {}).items() if v)
    c = s.get('control') or {}
    flags = ''
    if c.get('override'):
        flags += f" OVERRIDE({c.get('override_reason')})"
    if c.get('watchdog_tripped'):
        flags += ' WATCHDOG'
    if c.get('stopped'):
        flags += ' STOPPED'
    sliders = ' '.join(f'{k[0]}{v}' for k, v in sl.items())
    return (f"{mmss(s.get('time'))} BT {s.get('bt')} ET {s.get('et')} RoR {s.get('bt_ror')} | ref {ref} "
            f"SV {p.get('sv')} pid {'on' if p.get('active') else 'off'} gap {gap} | {sliders} | ev [{ev}]{flags}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dur', type=float, default=60)
    ap.add_argument('--every', type=float, default=15)
    ap.add_argument('--limit', type=float, default=None, help='BT safety limit (default 420F / 215C)')
    ap.add_argument('--port', type=int, default=8081)
    a = ap.parse_args()
    base = f'http://127.0.0.1:{a.port}/api/v1'
    try:
        s = call(base, '/state')
    except OSError as e:
        print(f'API unreachable: {e}')
        return 13
    limit = a.limit if a.limit is not None else (420.0 if s.get('unit') == 'F' else 215.0)
    print(line(s), flush=True)
    if not s.get('recording'):
        print('NOT RECORDING')
        return 14
    seen = {k for k, v in (s.get('events') or {}).items() if v}
    start = last = time.monotonic()
    failures = 0
    while time.monotonic() - start < a.dur:
        try:
            s = call(base, '/wait?timeout=5&until=sample')['state']
            failures = 0
        except (OSError, KeyError) as e:
            failures += 1
            if failures >= 3:
                print(f'API unreachable: {e}')
                return 13
            time.sleep(1)
            continue
        if s.get('bt') is not None and s['bt'] > limit:
            print(line(s))
            print(f"!!! BT {s['bt']} > {limit}: STOP -> {call(base, '/stop', {})}", flush=True)
            return 12
        events = {k for k, v in (s.get('events') or {}).items() if v}
        c = s.get('control') or {}
        new = events - seen
        locked = c.get('override') or c.get('watchdog_tripped') or c.get('stopped')
        if new or locked or time.monotonic() - last >= a.every:
            print(line(s), flush=True)
            last = time.monotonic()
        if new:
            print(f'NEW EVENT: {sorted(new)}')
            return 10
        if locked:
            return 11
        if not s.get('recording'):
            print('RECORDING STOPPED')
            return 14
    return 0


if __name__ == '__main__':
    sys.exit(main())
