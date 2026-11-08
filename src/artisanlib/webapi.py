#
# ABOUT
# Local HTTP control API for artisan scope
#
# LICENSE
# This program or module is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Local HTTP control API.

Exposes a small JSON API that lets a local client read the current roast state
and move the four event sliders / mark roast events, exactly as if the user had
done it in the UI. All writes are routed through existing queued Qt signals so
they execute in the GUI thread and fire the sliders' configured actions
(e.g. Kaleido, MODBUS, Hottop, ...), making the API device-agnostic.

Endpoints (all JSON):
  GET  /api/v1/state                         current snapshot
  GET  /api/v1/wait?timeout=30&until=event   long-poll; returns on event, next sample or timeout
  POST /api/v1/slider   {"slider": 0..3, "value": n}
  POST /api/v1/event    {"event": "CHARGE"|"DRY"|"FCs"|"FCe"|"SCs"|"SCe"|"DROP"|"COOL"}
  POST /api/v1/release  clears a manual-override or watchdog lock

Safety:
  * binds to 127.0.0.1 by default
  * optional bearer token
  * slider values clamped to the configured slider min/max
  * per-slider minimum interval between writes and optional maximum step size
  * manual override: if a slider is moved by anything other than the API, writes
    are refused until /release is called
  * watchdog: if armed and no request arrives within the timeout, an optional
    fallback slider value is applied and writes are locked until /release
"""

import math
import time
import asyncio
import logging
import json
from contextlib import suppress
from threading import Thread, Lock
from dataclasses import dataclass, field
from typing import Final, Any, Protocol, TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from artisanlib.main import ApplicationWindow # pylint: disable=unused-import


_log: Final[logging.Logger] = logging.getLogger(__name__)

API_VERSION: Final[int] = 1
EVENT_NAMES: Final[tuple[str, ...]] = ('CHARGE', 'DRY', 'FCs', 'FCe', 'SCs', 'SCe', 'DROP', 'COOL')
MAX_WAIT_TIMEOUT: Final[float] = 120.0
MANUAL_OVERRIDE_GRACE: Final[float] = 3.0 # seconds to wait for a queued slider move to land before comparing


class ArtisanAdapter(Protocol):
    """The minimal interface the API needs from Artisan. Implemented by AppAdapter;
    replaced by a fake in unit tests."""
    def snapshot(self) -> dict[str, Any]: ...
    def event_signature(self) -> tuple[int, ...]: ...
    def slider_count(self) -> int: ...
    def slider_value(self, n:int) -> int: ...
    def slider_limits(self, n:int) -> tuple[int, int]: ...
    def set_slider(self, n:int, value:int) -> None: ...
    def is_recording(self) -> bool: ...
    def mark_event(self, name:str) -> None: ...


@dataclass
class WebAPIConfig:
    host: str = '127.0.0.1'
    port: int = 8081
    token: str = ''                 # empty: no auth
    min_interval: float = 1.0       # minimum seconds between writes to the same slider (0: off)
    max_step: int = 0               # maximum change per write (0: unlimited)
    watchdog_timeout: float = 0.0   # seconds without any request before the watchdog trips (0: off)
    watchdog_slider: int = -1       # slider to set on trip (-1: just hold and lock)
    watchdog_value: int = 0         # value applied to watchdog_slider on trip


@dataclass
class ControlState:
    expected: dict[int, int] = field(default_factory=dict) # last value set via the API per slider
    last_write: dict[int, float] = field(default_factory=dict) # time of last write per slider
    override: bool = False
    override_reason: str = ''
    watchdog_armed: bool = False
    watchdog_tripped: bool = False
    last_seen: float = field(default_factory=time.monotonic)
    active_waits: int = 0


class WebAPI:

    def __init__(self, adapter:ArtisanAdapter, config:WebAPIConfig) -> None:
        self._adapter = adapter
        self._config = config
        self._control = ControlState()
        self._lock = Lock() # guards _control across the GUI and asyncio threads
        self._loop: asyncio.AbstractEventLoop|None = None
        self._thread: Thread|None = None
        self._runner: web.AppRunner|None = None
        self._sample_event: asyncio.Event|None = None
        self._watchdog_task: asyncio.Task[None]|None = None
        self.app = self.create_app()

    # ---------------------------------------------------------------- app

    def create_app(self) -> web.Application:
        app = web.Application(middlewares=[self._auth_middleware])
        app.add_routes([
            web.get('/api/v1/state', self.handle_state),
            web.get('/api/v1/wait', self.handle_wait),
            web.post('/api/v1/slider', self.handle_slider),
            web.post('/api/v1/event', self.handle_event),
            web.post('/api/v1/release', self.handle_release),
        ])
        app.on_startup.append(self._on_startup)
        app.on_cleanup.append(self._on_cleanup)
        return app

    async def _on_startup(self, _app:web.Application) -> None:
        self._sample_event = asyncio.Event()
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def _on_cleanup(self, _app:web.Application) -> None:
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._watchdog_task

    @web.middleware
    async def _auth_middleware(self, request:web.Request, handler:Any) -> web.StreamResponse:
        if self._config.token:
            auth = request.headers.get('Authorization', '')
            if auth != f'Bearer {self._config.token}':
                return self._error(401, 'unauthorized')
        with self._lock:
            self._control.last_seen = time.monotonic()
        try:
            res:web.StreamResponse = await handler(request)
            return res
        finally:
            with self._lock:
                self._control.last_seen = time.monotonic()

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _json(data:dict[str, Any], status:int = 200) -> web.Response:
        return web.json_response(data, status=status, dumps=lambda d: json.dumps(d, separators=(',', ':')))

    @classmethod
    def _error(cls, status:int, message:str) -> web.Response:
        return cls._json({'error': message}, status)

    def _control_info(self) -> dict[str, Any]:
        with self._lock:
            return {
                'override': self._control.override,
                'override_reason': self._control.override_reason,
                'watchdog_armed': self._control.watchdog_armed,
                'watchdog_tripped': self._control.watchdog_tripped,
                'watchdog_timeout': self._config.watchdog_timeout,
            }

    def _state(self) -> dict[str, Any]:
        self._check_manual_override()
        state = self._adapter.snapshot()
        state['api_version'] = API_VERSION
        state['control'] = self._control_info()
        return state

    def _check_manual_override(self) -> None:
        """Engage the override if a slider the API set has since been moved by something else."""
        now = time.monotonic()
        with self._lock:
            if self._control.override:
                return
            for n, expected in self._control.expected.items():
                if now - self._control.last_write.get(n, 0.0) < MANUAL_OVERRIDE_GRACE:
                    continue
                actual = self._adapter.slider_value(n)
                if actual != expected:
                    self._control.override = True
                    self._control.override_reason = f'slider {n} moved externally ({expected} -> {actual})'
                    _log.info('WebAPI manual override: %s', self._control.override_reason)
                    return

    @staticmethod
    async def _read_json(request:web.Request) -> dict[str, Any]|None:
        try:
            data = await request.json()
            return data if isinstance(data, dict) else None
        except Exception: # pylint: disable=broad-except
            return None

    # ---------------------------------------------------------------- handlers

    async def handle_state(self, _request:web.Request) -> web.Response:
        return self._json(self._state())

    async def handle_wait(self, request:web.Request) -> web.Response:
        try:
            timeout = min(max(float(request.query.get('timeout', '30')), 0.0), MAX_WAIT_TIMEOUT)
        except ValueError:
            return self._error(400, 'timeout must be a number')
        until = request.query.get('until', 'event')
        if until not in {'event', 'sample'}:
            return self._error(400, "until must be 'event' or 'sample'")

        start_sig = self._adapter.event_signature()
        deadline = time.monotonic() + timeout
        reason = 'timeout'
        with self._lock:
            self._control.active_waits += 1
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                event = self._sample_event
                if event is None:
                    await asyncio.sleep(min(remaining, 1.0))
                    continue
                try:
                    await asyncio.wait_for(event.wait(), remaining)
                except TimeoutError:
                    break
                if self._adapter.event_signature() != start_sig:
                    reason = 'event'
                    break
                if until == 'sample':
                    reason = 'sample'
                    break
        finally:
            with self._lock:
                self._control.active_waits -= 1
        return self._json({'reason': reason, 'state': self._state()})

    async def handle_slider(self, request:web.Request) -> web.Response:
        data = await self._read_json(request)
        if data is None:
            return self._error(400, 'expected a JSON object')
        n = data.get('slider')
        value = data.get('value')
        if not isinstance(n, int) or isinstance(n, bool) or not 0 <= n < self._adapter.slider_count():
            return self._error(400, f'slider must be an integer 0..{self._adapter.slider_count() - 1}')
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return self._error(400, 'value must be a number')

        self._check_manual_override()
        now = time.monotonic()
        lo, hi = self._adapter.slider_limits(n)
        target = int(min(max(round(value), lo), hi))
        with self._lock:
            if self._control.watchdog_tripped:
                return self._error(409, 'watchdog tripped; POST /api/v1/release to resume')
            if self._control.override:
                return self._error(409, f'manual override active ({self._control.override_reason}); POST /api/v1/release to resume')
            last = self._control.last_write.get(n)
            if self._config.min_interval > 0 and last is not None and now - last < self._config.min_interval:
                return self._error(429, f'slider {n} written less than {self._config.min_interval}s ago')
            current = self._adapter.slider_value(n)
            if self._config.max_step > 0 and abs(target - current) > self._config.max_step:
                return self._error(422, f'step {abs(target - current)} exceeds max_step {self._config.max_step}')
            self._control.expected[n] = target
            self._control.last_write[n] = now
            if self._adapter.is_recording():
                self._control.watchdog_armed = self._config.watchdog_timeout > 0
        self._adapter.set_slider(n, target)
        return self._json({'slider': n, 'requested': value, 'value': target, 'clamped': target != value})

    async def handle_event(self, request:web.Request) -> web.Response:
        data = await self._read_json(request)
        if data is None:
            return self._error(400, 'expected a JSON object')
        name = data.get('event')
        if name not in EVENT_NAMES:
            return self._error(400, f"event must be one of {', '.join(EVENT_NAMES)}")
        if not self._adapter.is_recording():
            return self._error(409, 'not recording')
        self._adapter.mark_event(name)
        return self._json({'event': name})

    async def handle_release(self, _request:web.Request) -> web.Response:
        with self._lock:
            self._control.override = False
            self._control.override_reason = ''
            self._control.watchdog_tripped = False
            # re-baseline expectations against the current slider positions
            for n in list(self._control.expected):
                self._control.expected[n] = self._adapter.slider_value(n)
        return self._json({'control': self._control_info()})

    # ---------------------------------------------------------------- watchdog

    def watchdog_tick(self) -> bool:
        """Checks the watchdog; returns True if it tripped on this tick. Exposed for tests."""
        with self._lock:
            c = self._control
            if not c.watchdog_armed or c.watchdog_tripped or c.active_waits > 0 or self._config.watchdog_timeout <= 0:
                return False
            if not self._adapter.is_recording():
                c.watchdog_armed = False
                return False
            if time.monotonic() - c.last_seen < self._config.watchdog_timeout:
                return False
            c.watchdog_tripped = True
            n = self._config.watchdog_slider
            value = 0
            apply_fallback = 0 <= n < self._adapter.slider_count()
            if apply_fallback:
                lo, hi = self._adapter.slider_limits(n)
                value = int(min(max(self._config.watchdog_value, lo), hi))
                c.expected[n] = value
                c.last_write[n] = time.monotonic()
        _log.warning('WebAPI watchdog tripped after %ss without requests', self._config.watchdog_timeout)
        if apply_fallback:
            self._adapter.set_slider(n, value)
        return True

    async def _watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                self.watchdog_tick()
            except Exception as e: # pylint: disable=broad-except
                _log.exception(e)

    # ---------------------------------------------------------------- sampling hook

    def notify_sample(self) -> None:
        """Called from the GUI thread after each sample; wakes long-poll waiters."""
        loop = self._loop
        if loop is not None:
            with suppress(RuntimeError): # loop closed
                loop.call_soon_threadsafe(self._pulse)

    def _pulse(self) -> None:
        event = self._sample_event
        if event is not None:
            event.set()
            # waiters woken by this set() re-check and then wait on the new event
            self._sample_event = asyncio.Event()

    # ---------------------------------------------------------------- lifecycle

    async def _startup(self) -> None:
        self._runner = web.AppRunner(self.app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._config.host, self._config.port)
        await asyncio.wait_for(site.start(), 0.7)

    @staticmethod
    def _run_loop(loop:asyncio.AbstractEventLoop) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
            for task in asyncio.all_tasks(loop):
                task.cancel()
            for t in [t for t in asyncio.all_tasks(loop) if not (t.done() or t.cancelled())]:
                with suppress(asyncio.CancelledError):
                    loop.run_until_complete(t)
        except Exception as e: # pylint: disable=broad-except
            _log.exception(e)
        finally:
            loop.close()

    def start(self) -> bool:
        _log.info('start WebAPI on %s:%s', self._config.host, self._config.port)
        self._loop = asyncio.new_event_loop()
        self._thread = Thread(target=self._run_loop, args=(self._loop,), daemon=True)
        self._thread.start()
        asyncio.run_coroutine_threadsafe(self._startup(), self._loop).result()
        return True

    def stop(self) -> None:
        _log.info('stop WebAPI')
        loop = self._loop
        if loop is not None:
            if self._runner is not None:
                with suppress(Exception):
                    asyncio.run_coroutine_threadsafe(self._runner.cleanup(), loop).result(2)
            loop.call_soon_threadsafe(loop.stop)
            self._loop = None
        if self._thread is not None:
            self._thread.join()
            self._thread = None


class AppAdapter:
    """Binds the API to the running Artisan ApplicationWindow.

    Reads happen on the API thread against lists the sampling/GUI threads append to;
    each read takes a length snapshot first so an append mid-read is harmless.
    Writes go through queued Qt signals and therefore execute in the GUI thread."""

    def __init__(self, aw:'ApplicationWindow') -> None:
        self.aw = aw

    def slider_count(self) -> int: # pylint: disable=no-self-use
        return 4

    def slider_value(self, n:int) -> int:
        return int(self.aw.eventslidervalues[n])

    def slider_limits(self, n:int) -> tuple[int, int]:
        return int(self.aw.eventslidermin[n]), int(self.aw.eventslidermax[n])

    def is_recording(self) -> bool:
        return bool(self.aw.qmc.flagstart)

    def event_signature(self) -> tuple[int, ...]:
        return tuple(self.aw.qmc.timeindex)

    def set_slider(self, n:int, value:int) -> None:
        # value, etype, record, fire_slider_action, force
        self.aw.addEventSignal.emit(value, n, True, True, False)

    def mark_event(self, name:str) -> None:
        qmc = self.aw.qmc
        signals = {
            'CHARGE': qmc.markChargeSignal,
            'DRY': qmc.markDRYSignal,
            'FCs': qmc.markFCsSignal,
            'FCe': qmc.markFCeSignal,
            'SCs': qmc.markSCsSignal,
            'SCe': qmc.markSCeSignal,
            'DROP': qmc.markDropSignal,
            'COOL': qmc.markCoolSignal,
        }
        signals[name].emit(False) # noaction=False: fire the configured button actions as a user click would

    @staticmethod
    def _r(v:float|None, digits:int = 1) -> float|None:
        if v is None or v == -1 or math.isnan(v): # None, Artisan's -1 'no value', NaN
            return None
        return round(float(v), digits)

    def snapshot(self) -> dict[str, Any]:
        aw = self.aw
        qmc = aw.qmc
        recording = bool(qmc.flagstart)
        timex = qmc.timex if recording else qmc.on_timex
        temp1 = qmc.temp1 if recording else qmc.on_temp1
        temp2 = qmc.temp2 if recording else qmc.on_temp2
        length = min(len(timex), len(temp1), len(temp2))
        timeindex = list(qmc.timeindex)
        charge_idx = timeindex[0]
        charged = recording and -1 < charge_idx < length
        t0 = timex[charge_idx] if charged else (timex[0] if length else 0.0)

        state:dict[str, Any] = {
            'unit': qmc.mode,
            'monitoring': bool(qmc.flagon),
            'recording': recording,
            'charged': charged,
            'time': round(timex[length - 1] - t0, 1) if length else None,
            'bt': self._r(temp2[length - 1]) if length else None,
            'et': self._r(temp1[length - 1]) if length else None,
            'bt_ror': self._r(qmc.rateofchange2),
            'et_ror': self._r(qmc.rateofchange1),
            'bt_background': None,
        }
        if charged and qmc.background and length:
            with suppress(Exception):
                state['bt_background'] = self._r(qmc.backgroundSmoothedBTat(timex[length - 1] - t0))

        events:dict[str, Any] = {}
        if recording:
            for i, name in enumerate(EVENT_NAMES):
                idx = timeindex[i] if i < len(timeindex) else 0
                is_set = (-1 < idx < length) if i == 0 else (0 < idx < length)
                events[name] = {'time': round(timex[idx] - t0, 1), 'bt': self._r(temp2[idx])} if is_set and charged else None
        state['events'] = events

        state['sliders'] = [
            {
                'slider': n,
                'name': str(qmc.etypes[n]),
                'value': int(aw.eventslidervalues[n]),
                'min': int(aw.eventslidermin[n]),
                'max': int(aw.eventslidermax[n]),
                'visible': bool(aw.eventslidervisibilities[n]),
            } for n in range(4)
        ]
        return state
