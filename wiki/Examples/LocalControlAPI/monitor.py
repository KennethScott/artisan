"""Minimal Artisan Local Control API monitor (Python 3.10+, standard library only).

Prints one line per `--every` seconds and on each roast event, e.g.

  03:12 BT 330.3 ET 333.1 RoR +23.7  ref 335.4 (-5.1)  SV 332.9 PID on   Burner 35 Air 20
  04:41 *** FCs at BT 384.2 ***

usage: python monitor.py [--port 8081] [--token TOKEN] [--every 10]
"""

import argparse
import json
import time
import urllib.request
from typing import Any


def get(base: str, path: str, token: str) -> dict[str, Any]:
    req = urllib.request.Request(base + path)
    if token:
        req.add_header('Authorization', f'Bearer {token}')
    with urllib.request.urlopen(req, timeout=40) as r:
        return json.loads(r.read())


def mmss(t: float | None) -> str:
    return '--:--' if t is None else f'{int(t) // 60:02d}:{int(t) % 60:02d}'


def line(s: dict[str, Any]) -> str:
    parts = [mmss(s.get('time'))]
    for key, label in (('bt', 'BT'), ('et', 'ET')):
        if s.get(key) is not None:
            parts.append(f'{label} {s[key]:.1f}')
    if s.get('bt_ror') is not None:
        parts.append(f"RoR {s['bt_ror']:+.1f}")
    ref = s.get('bt_background')
    if ref is not None and s.get('bt') is not None:
        parts.append(f" ref {ref:.1f} ({s['bt'] - ref:+.1f})")
    pid = s.get('pid') or {}
    if pid.get('available'):
        sv = pid.get('sv')
        parts.append(f" SV {sv:.1f}" if sv is not None else ' SV -')
        parts.append(f"PID {'on ' if pid.get('active') else 'off'}")
    for sl in s.get('sliders', []):
        if sl.get('visible'):
            parts.append(f"{sl['name']} {sl['value']}")
    c = s.get('control') or {}
    if c.get('override'):
        parts.append(f"[locked: {c.get('override_reason')}]")
    return ' '.join(parts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8081)
    ap.add_argument('--token', default='')
    ap.add_argument('--every', type=float, default=10.0)
    args = ap.parse_args()
    base = f'http://127.0.0.1:{args.port}/api/v1'

    state = get(base, '/state', args.token)
    seen = {k for k, v in (state.get('events') or {}).items() if v}
    print(line(state), flush=True)
    last = time.monotonic()
    while True:
        res = get(base, '/wait?timeout=30&until=sample', args.token)
        state = res['state']
        events = state.get('events') or {}
        for name, ev in events.items():
            if ev and name not in seen:
                seen.add(name)
                print(f"{mmss(ev['time'])} *** {name} at BT {ev['bt']} ***", flush=True)
        if time.monotonic() - last >= args.every:
            print(line(state), flush=True)
            last = time.monotonic()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
