"""Unit tests for artisanlib.webapi (local control API).

Uses a fake adapter so no Qt / ApplicationWindow is needed for the HTTP layer,
plus a Mock-based ApplicationWindow to check AppAdapter's mapping onto Artisan.
"""

import asyncio
import time
from typing import Any
from unittest.mock import Mock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from artisanlib.webapi import WebAPI, WebAPIConfig, AppAdapter, MANUAL_OVERRIDE_GRACE, FALLBACK_NONE


class FakeAdapter:
    def __init__(self) -> None:
        self.values = [0, 0, 0, 0]
        self.limits = [(0, 100)] * 4
        self.recording = True
        self.timeindex = [0, 0, 0, 0, 0, 0, 0, 0]
        self.steps = [1, 1, 1, 1]
        self.tolerances = [1, 1, 1, 1]
        self.set_calls: list[tuple[int, int]] = []
        self.events: list[str] = []
        self.pid: dict[str, Any] = {'available': True, 'active': False, 'sv': None, 'sv_min': 0.0, 'sv_max': 446.0,
                                    'mode': 'manual', 'source': 'BT', 'kind': 'kaleido'}
        self.sv_calls: list[float] = []
        self.pid_calls: list[bool] = []
        self.heat: int|None = None
        self.bg: dict[str, Any] = {'loaded': True, 'title': 'ref', 'charged': True, 'shift_temp': 0, 'shift_time': 0, 'unit': 'F'}
        self.targets: tuple[float, float|None]|None = None
        self.mode_calls: list[str] = []
        self.lookahead_calls: list[int] = []
        self.calls: list[str] = [] # order of PID-related calls

    def snapshot(self) -> dict[str, Any]:
        return {'bt': 200.0, 'sliders': list(self.values)}
    def event_signature(self) -> tuple[int, ...]:
        return tuple(self.timeindex)
    def slider_count(self) -> int:
        return 4
    def slider_value(self, n:int) -> int:
        return self.values[n]
    def slider_limits(self, n:int) -> tuple[int, int]:
        return self.limits[n]
    def slider_step(self, n:int) -> int:
        return self.steps[n]
    def slider_tolerance(self, n:int) -> int:
        return self.tolerances[n]
    def heat_slider(self) -> int|None:
        return self.heat
    def set_slider(self, n:int, value:int) -> None:
        self.set_calls.append((n, value))
        self.values[n] = value # the real adapter moves the slider asynchronously in the GUI thread
    def pid_info(self) -> dict[str, Any]:
        return dict(self.pid)
    def set_sv(self, value:float) -> None:
        self.sv_calls.append(value)
        self.pid['sv'] = value
    def set_pid(self, on:bool) -> None:
        self.pid_calls.append(on)
        self.calls.append(f'pid {on}')
        self.pid['active'] = on
    def set_pid_mode(self, mode:str) -> None:
        self.mode_calls.append(mode)
        self.calls.append(f'mode {mode}')
        self.pid['mode'] = mode
    def set_pid_lookahead(self, seconds:int) -> None:
        self.lookahead_calls.append(seconds)
        self.calls.append(f'lookahead {seconds}')
    def background_info(self) -> dict[str, Any]:
        return dict(self.bg)
    def background_targets(self, time_s:int) -> tuple[float, float|None]|None:
        del time_s
        return self.targets
    def shift_background(self, temp:int, time_s:int) -> None:
        self.bg['shift_temp'] += temp
        self.bg['shift_time'] += time_s
    def is_recording(self) -> bool:
        return self.recording
    def mark_event(self, name:str) -> None:
        self.events.append(name)


async def make_client(api:WebAPI) -> TestClient: # type: ignore[type-arg]
    client = TestClient(TestServer(api.app))
    await client.start_server()
    return client


@pytest.fixture
def adapter() -> FakeAdapter:
    return FakeAdapter()


@pytest.mark.asyncio
async def test_state(adapter:FakeAdapter) -> None:
    client = await make_client(WebAPI(adapter, WebAPIConfig()))
    try:
        resp = await client.get('/api/v1/state')
        assert resp.status == 200
        data = await resp.json()
        assert data['api_version'] == 1
        assert data['bt'] == 200.0
        assert data['control']['override'] is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_token_required(adapter:FakeAdapter) -> None:
    client = await make_client(WebAPI(adapter, WebAPIConfig(token='s3cret')))
    try:
        assert (await client.get('/api/v1/state')).status == 401
        assert (await client.get('/api/v1/state', headers={'Authorization': 'Bearer wrong'})).status == 401
        assert (await client.get('/api/v1/state', headers={'Authorization': 'Bearer s3cret'})).status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_slider_clamps_and_sets(adapter:FakeAdapter) -> None:
    adapter.limits[1] = (10, 80)
    client = await make_client(WebAPI(adapter, WebAPIConfig(min_interval=0)))
    try:
        resp = await client.post('/api/v1/slider', json={'slider': 1, 'value': 95})
        assert resp.status == 200
        data = await resp.json()
        assert data['value'] == 80 and data['clamped'] is True
        assert adapter.set_calls == [(1, 80)]
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [
    {'slider': 4, 'value': 10},
    {'slider': -1, 'value': 10},
    {'slider': True, 'value': 10},
    {'slider': 0, 'value': 'hot'},
    {'slider': 0},
    [1, 2],
])
async def test_slider_validation(adapter:FakeAdapter, body:Any) -> None:
    client = await make_client(WebAPI(adapter, WebAPIConfig()))
    try:
        assert (await client.post('/api/v1/slider', json=body)).status == 400
        assert not adapter.set_calls
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_slider_rate_limit(adapter:FakeAdapter) -> None:
    client = await make_client(WebAPI(adapter, WebAPIConfig(min_interval=10)))
    try:
        assert (await client.post('/api/v1/slider', json={'slider': 0, 'value': 50})).status == 200
        assert (await client.post('/api/v1/slider', json={'slider': 0, 'value': 55})).status == 429
        # other sliders are limited independently
        assert (await client.post('/api/v1/slider', json={'slider': 1, 'value': 55})).status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_slider_max_step(adapter:FakeAdapter) -> None:
    adapter.values[0] = 50
    client = await make_client(WebAPI(adapter, WebAPIConfig(min_interval=0, max_step=10)))
    try:
        assert (await client.post('/api/v1/slider', json={'slider': 0, 'value': 70})).status == 422
        assert (await client.post('/api/v1/slider', json={'slider': 0, 'value': 60})).status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_manual_override_locks_until_release(adapter:FakeAdapter) -> None:
    api = WebAPI(adapter, WebAPIConfig(min_interval=0))
    client = await make_client(api)
    try:
        assert (await client.post('/api/v1/slider', json={'slider': 0, 'value': 50})).status == 200
        # user moves the slider by hand after the grace period
        adapter.values[0] = 30
        api._control.last_write[0] = time.monotonic() - MANUAL_OVERRIDE_GRACE - 1 # pylint: disable=protected-access
        resp = await client.post('/api/v1/slider', json={'slider': 0, 'value': 55})
        assert resp.status == 409
        state = await (await client.get('/api/v1/state')).json()
        assert state['control']['override'] is True
        assert (await client.post('/api/v1/release')).status == 200
        assert (await client.post('/api/v1/slider', json={'slider': 0, 'value': 55})).status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_no_override_within_grace(adapter:FakeAdapter) -> None:
    client = await make_client(WebAPI(adapter, WebAPIConfig(min_interval=0)))
    try:
        assert (await client.post('/api/v1/slider', json={'slider': 0, 'value': 50})).status == 200
        adapter.values[0] = 0 # queued move not yet applied
        state = await (await client.get('/api/v1/state')).json()
        assert state['control']['override'] is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_event(adapter:FakeAdapter) -> None:
    client = await make_client(WebAPI(adapter, WebAPIConfig()))
    try:
        assert (await client.post('/api/v1/event', json={'event': 'FCs'})).status == 200
        assert (await client.post('/api/v1/event', json={'event': 'BOOM'})).status == 400
        adapter.recording = False
        assert (await client.post('/api/v1/event', json={'event': 'DROP'})).status == 409
        assert adapter.events == ['FCs']
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_wait_timeout(adapter:FakeAdapter) -> None:
    client = await make_client(WebAPI(adapter, WebAPIConfig()))
    try:
        data = await (await client.get('/api/v1/wait', params={'timeout': '0.2'})).json()
        assert data['reason'] == 'timeout'
        assert (await client.get('/api/v1/wait', params={'until': 'never'})).status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_wait_returns_on_sample_and_event(adapter:FakeAdapter) -> None:
    api = WebAPI(adapter, WebAPIConfig())
    client = await make_client(api)
    try:
        async def pulse_later(change_event:bool) -> None:
            await asyncio.sleep(0.1)
            if change_event:
                adapter.timeindex[2] = 42 # FCs marked
            api._pulse() # pylint: disable=protected-access

        task = asyncio.create_task(pulse_later(False))
        data = await (await client.get('/api/v1/wait', params={'timeout': '5', 'until': 'sample'})).json()
        await task
        assert data['reason'] == 'sample'

        # until=event ignores plain samples but returns once an event index changes
        task = asyncio.create_task(pulse_later(False))
        data = await (await client.get('/api/v1/wait', params={'timeout': '0.4', 'until': 'event'})).json()
        await task
        assert data['reason'] == 'timeout'

        task = asyncio.create_task(pulse_later(True))
        data = await (await client.get('/api/v1/wait', params={'timeout': '5', 'until': 'event'})).json()
        await task
        assert data['reason'] == 'event'
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_watchdog_trips_applies_fallback_and_locks(adapter:FakeAdapter) -> None:
    api = WebAPI(adapter, WebAPIConfig(min_interval=0, watchdog_timeout=5, watchdog_slider=2, watchdog_value=20))
    client = await make_client(api)
    try:
        assert (await client.post('/api/v1/slider', json={'slider': 2, 'value': 70})).status == 200
        assert api.watchdog_tick() is False # fresh request
        api._control.last_seen = time.monotonic() - 10 # pylint: disable=protected-access
        assert api.watchdog_tick() is True
        assert adapter.set_calls[-1] == (2, 20)
        assert api.watchdog_tick() is False # only trips once
        assert (await client.post('/api/v1/slider', json={'slider': 2, 'value': 70})).status == 409
        assert (await client.post('/api/v1/release')).status == 200
        assert (await client.post('/api/v1/slider', json={'slider': 2, 'value': 70})).status == 200
    finally:
        await client.close()


def test_watchdog_not_armed_without_recording(adapter:FakeAdapter) -> None:
    api = WebAPI(adapter, WebAPIConfig(watchdog_timeout=1))
    api._control.last_seen = time.monotonic() - 10 # pylint: disable=protected-access
    assert api.watchdog_tick() is False


def _age_writes(api:WebAPI) -> None:
    """Pretend all API writes happened before the manual-override grace period."""
    old = time.monotonic() - MANUAL_OVERRIDE_GRACE - 1
    c = api._control # pylint: disable=protected-access
    for n in c.last_write:
        c.last_write[n] = old
    if c.last_sv_write is not None:
        c.last_sv_write = old
    if c.last_pid_write is not None:
        c.last_pid_write = old


@pytest.mark.asyncio
async def test_slider_snaps_to_step(adapter:FakeAdapter) -> None:
    adapter.steps[3] = 5
    client = await make_client(WebAPI(adapter, WebAPIConfig(min_interval=0)))
    try:
        data = await (await client.post('/api/v1/slider', json={'slider': 3, 'value': 37})).json()
        assert data['value'] == 35 and data['clamped'] is True
        data = await (await client.post('/api/v1/slider', json={'slider': 3, 'value': 38})).json()
        assert data['value'] == 40
        assert adapter.set_calls == [(3, 35), (3, 40)]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_override_tolerates_quantifier_rounding(adapter:FakeAdapter) -> None:
    adapter.tolerances[1] = 10 # quantifier in steps of 10 follows the machine's drum reading
    api = WebAPI(adapter, WebAPIConfig(min_interval=0))
    client = await make_client(api)
    try:
        assert (await client.post('/api/v1/slider', json={'slider': 1, 'value': 20})).status == 200
        _age_writes(api)
        adapter.values[1] = 25 # rounding noise below one quantifier step
        assert (await (await client.get('/api/v1/state')).json())['control']['override'] is False
        adapter.values[1] = 40 # the machine really changed the drum speed
        state = await (await client.get('/api/v1/state')).json()
        assert state['control']['override'] is True
        assert 'slider 1' in state['control']['override_reason']
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_sv_sets_and_clamps(adapter:FakeAdapter) -> None:
    client = await make_client(WebAPI(adapter, WebAPIConfig(min_interval=0)))
    try:
        data = await (await client.post('/api/v1/sv', json={'value': 380.04})).json()
        assert data['sv'] == 380.0 and data['clamped'] is True
        data = await (await client.post('/api/v1/sv', json={'value': 999})).json()
        assert data['sv'] == 446.0
        assert adapter.sv_calls == [380.0, 446.0]
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('body', [{'value': 'hot'}, {'value': True}, {}, [1]])
async def test_sv_validation(adapter:FakeAdapter, body:Any) -> None:
    client = await make_client(WebAPI(adapter, WebAPIConfig()))
    try:
        assert (await client.post('/api/v1/sv', json=body)).status == 400
        assert not adapter.sv_calls
    finally:
        await client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('raw', ['{"value": NaN}', '{"value": Infinity}'])
async def test_sv_rejects_non_finite(adapter:FakeAdapter, raw:str) -> None:
    client = await make_client(WebAPI(adapter, WebAPIConfig()))
    try:
        resp = await client.post('/api/v1/sv', data=raw, headers={'Content-Type': 'application/json'})
        assert resp.status == 400
        assert not adapter.sv_calls
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_sv_refused_without_pid_or_outside_manual_mode(adapter:FakeAdapter) -> None:
    client = await make_client(WebAPI(adapter, WebAPIConfig()))
    try:
        adapter.pid['mode'] = 'background'
        resp = await client.post('/api/v1/sv', json={'value': 300})
        assert resp.status == 409 and 'background' in (await resp.json())['error']
        adapter.pid['available'] = False
        assert (await client.post('/api/v1/sv', json={'value': 300})).status == 409
        assert (await client.post('/api/v1/pid', json={'on': True})).status == 409
        assert not adapter.sv_calls and not adapter.pid_calls
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_sv_rate_limit_and_max_step(adapter:FakeAdapter) -> None:
    adapter.pid['sv'] = 300.0
    client = await make_client(WebAPI(adapter, WebAPIConfig(min_interval=10, max_sv_step=10)))
    try:
        assert (await client.post('/api/v1/sv', json={'value': 320})).status == 422
        assert (await client.post('/api/v1/sv', json={'value': 310})).status == 200
        assert (await client.post('/api/v1/sv', json={'value': 312})).status == 429
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_sv_external_change_engages_override(adapter:FakeAdapter) -> None:
    api = WebAPI(adapter, WebAPIConfig(min_interval=0))
    client = await make_client(api)
    try:
        assert (await client.post('/api/v1/sv', json={'value': 350})).status == 200
        _age_writes(api)
        adapter.pid['sv'] = 350.3 # below tolerance
        assert (await (await client.get('/api/v1/state')).json())['control']['override'] is False
        adapter.pid['sv'] = 370.0 # user moved the SV slider
        resp = await client.post('/api/v1/sv', json={'value': 355})
        assert resp.status == 409 and 'SV changed' in (await resp.json())['error']
        assert (await client.post('/api/v1/release')).status == 200
        assert (await client.post('/api/v1/sv', json={'value': 360})).status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_sv_tolerance_from_pid_info(adapter:FakeAdapter) -> None:
    adapter.pid['sv_tolerance'] = 3.0 # e.g. Kaleido: machine keeps whole degrees C
    api = WebAPI(adapter, WebAPIConfig(min_interval=0))
    client = await make_client(api)
    try:
        assert (await client.post('/api/v1/sv', json={'value': 155})).status == 200
        _age_writes(api)
        adapter.pid['sv'] = 153.0 # machine settled on 67C
        assert (await (await client.get('/api/v1/state')).json())['control']['override'] is False
        adapter.pid['sv'] = 140.0
        assert (await (await client.get('/api/v1/state')).json())['control']['override'] is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stop_applies_fallback_even_when_locked(adapter:FakeAdapter) -> None:
    adapter.values[3] = 80
    api = WebAPI(adapter, WebAPIConfig(min_interval=0, watchdog_slider=3, watchdog_value=0))
    client = await make_client(api)
    try:
        assert (await client.post('/api/v1/pid', json={'on': True})).status == 200
        assert (await client.post('/api/v1/slider', json={'slider': 0, 'value': 50})).status == 200
        _age_writes(api)
        adapter.values[0] = 20 # user took over: API is locked
        assert (await client.post('/api/v1/slider', json={'slider': 0, 'value': 55})).status == 409
        data = await (await client.post('/api/v1/stop')).json()
        assert data['actions'] == ['pid off', 'slider 3 = 0']
        assert data['control']['stopped'] is True
        assert adapter.pid_calls[-1] is False and adapter.set_calls[-1] == (3, 0)
        # everything stays locked, also after the override is gone, until release
        assert (await client.post('/api/v1/release')).status == 200
        assert (await (await client.get('/api/v1/state')).json())['control']['stopped'] is False
        assert (await client.post('/api/v1/slider', json={'slider': 0, 'value': 55})).status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stop_locks_writes(adapter:FakeAdapter) -> None:
    client = await make_client(WebAPI(adapter, WebAPIConfig(min_interval=0, watchdog_slider=3)))
    try:
        assert (await client.post('/api/v1/stop')).status == 200
        resp = await client.post('/api/v1/sv', json={'value': 300})
        assert resp.status == 409 and 'stopped' in (await resp.json())['error']
        assert (await client.post('/api/v1/pid', json={'on': True})).status == 409
        assert (await client.post('/api/v1/pid', json={'on': False})).status == 200 # off still fine
        assert (await client.post('/api/v1/stop')).status == 200 # stop is idempotent
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stop_auto_fallback_uses_pid_heat_slider_minimum(adapter:FakeAdapter) -> None:
    adapter.heat = 3
    adapter.limits[3] = (10, 100)
    api = WebAPI(adapter, WebAPIConfig()) # watchdog_slider defaults to auto
    client = await make_client(api)
    try:
        state = await (await client.get('/api/v1/state')).json()
        assert state['control']['fallback'] == {'slider': 3, 'value': 10}
        data = await (await client.post('/api/v1/stop')).json()
        assert data['actions'] == ['pid off', 'slider 3 = 10'] and 'warning' not in data
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_stop_auto_fallback_without_heat_slider_warns(adapter:FakeAdapter) -> None:
    client = await make_client(WebAPI(adapter, WebAPIConfig()))
    try:
        state = await (await client.get('/api/v1/state')).json()
        assert state['control']['fallback'] is None
        data = await (await client.post('/api/v1/stop')).json()
        assert data['actions'] == ['pid off'] and 'only the PID' in data['warning']
    finally:
        await client.close()


def test_explicit_fallback_overrides_auto(adapter:FakeAdapter) -> None:
    adapter.heat = 3
    assert WebAPI(adapter, WebAPIConfig(watchdog_slider=0, watchdog_value=150)).fallback() == (0, 100) # clamped
    assert WebAPI(adapter, WebAPIConfig(watchdog_slider=FALLBACK_NONE)).fallback() is None


@pytest.mark.asyncio
async def test_stop_without_anything_configured_warns(adapter:FakeAdapter) -> None:
    adapter.pid['available'] = False
    client = await make_client(WebAPI(adapter, WebAPIConfig()))
    try:
        data = await (await client.post('/api/v1/stop')).json()
        assert data['actions'] == [] and 'warning' in data
        assert not adapter.set_calls and not adapter.pid_calls
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pid_owns_heat_slider_while_active(adapter:FakeAdapter) -> None:
    adapter.heat = 3
    api = WebAPI(adapter, WebAPIConfig(min_interval=0))
    client = await make_client(api)
    try:
        assert (await client.post('/api/v1/stop')).status == 200 # sets heat slider 3 = 0 and tracks it
        assert (await client.post('/api/v1/release')).status == 200
        assert (await client.post('/api/v1/pid', json={'on': True})).status == 200
        _age_writes(api)
        adapter.values[3] = 70 # the PID drives the heat slider
        assert (await (await client.get('/api/v1/state')).json())['control']['override'] is False
        resp = await client.post('/api/v1/slider', json={'slider': 3, 'value': 20})
        assert resp.status == 409 and 'driven by the active PID' in (await resp.json())['error']
        assert (await client.post('/api/v1/slider', json={'slider': 0, 'value': 20})).status == 200 # others fine
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pid_mode_and_lookahead(adapter:FakeAdapter) -> None:
    client = await make_client(WebAPI(adapter, WebAPIConfig(min_interval=0)))
    try:
        resp = await client.post('/api/v1/pid', json={'mode': 'background', 'lookahead': 5, 'on': True})
        assert resp.status == 200
        assert await resp.json() == {'pid': True, 'mode': 'background', 'lookahead': 5}
        assert adapter.calls == ['lookahead 5', 'mode background', 'pid True'] # mode before on
        assert (await client.post('/api/v1/pid', json={})).status == 400
        assert (await client.post('/api/v1/pid', json={'mode': 'ramp_soak'})).status == 400
        assert (await client.post('/api/v1/pid', json={'lookahead': 61})).status == 400
        assert (await client.post('/api/v1/pid', json={'lookahead': True})).status == 400
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pid_background_mode_needs_background(adapter:FakeAdapter) -> None:
    adapter.bg['loaded'] = False
    client = await make_client(WebAPI(adapter, WebAPIConfig()))
    try:
        assert (await client.post('/api/v1/pid', json={'mode': 'background'})).status == 409
        assert (await client.post('/api/v1/background', json={'shift_temp': 5})).status == 409
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pid_off_with_mode_is_not_exempt_from_locks(adapter:FakeAdapter) -> None:
    api = WebAPI(adapter, WebAPIConfig(min_interval=0))
    client = await make_client(api)
    try:
        assert (await client.post('/api/v1/stop')).status == 200
        assert (await client.post('/api/v1/pid', json={'on': False, 'mode': 'manual'})).status == 409
        assert (await client.post('/api/v1/pid', json={'on': False})).status == 200
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_switching_to_background_stops_tracking_sv(adapter:FakeAdapter) -> None:
    api = WebAPI(adapter, WebAPIConfig(min_interval=0))
    client = await make_client(api)
    try:
        assert (await client.post('/api/v1/sv', json={'value': 300})).status == 200
        assert (await client.post('/api/v1/pid', json={'mode': 'background'})).status == 200
        _age_writes(api)
        adapter.pid['sv'] = 352.0 # SV now follows the background curve
        assert (await (await client.get('/api/v1/state')).json())['control']['override'] is False
        adapter.pid['mode'] = 'manual' # user switched the mode back
        state = await (await client.get('/api/v1/state')).json()
        assert 'SV mode changed externally' in state['control']['override_reason']
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_background_shift(adapter:FakeAdapter) -> None:
    api = WebAPI(adapter, WebAPIConfig(min_interval=10, max_sv_step=10))
    client = await make_client(api)
    try:
        assert (await client.post('/api/v1/background', json={'shift_temp': 15})).status == 422
        assert (await client.post('/api/v1/background', json={'shift_time': 121})).status == 422
        assert (await client.post('/api/v1/background', json={})).status == 400
        assert (await client.post('/api/v1/background', json={'shift_temp': 2.5})).status == 400
        data = await (await client.post('/api/v1/background', json={'shift_temp': 5, 'shift_time': -30})).json()
        assert data == {'shift_temp': 5, 'shift_time': -30}
        assert (await client.post('/api/v1/background', json={'shift_temp': 5})).status == 429
        _age_writes(api)
        api._control.last_shift_write = 0.0 # pylint: disable=protected-access
        assert (await (await client.get('/api/v1/state')).json())['control']['override'] is False
        adapter.bg['shift_temp'] = 0 # user moved the background back
        state = await (await client.get('/api/v1/state')).json()
        assert 'background moved externally' in state['control']['override_reason']
        assert (await client.post('/api/v1/release')).status == 200
        assert api._control.expected_shift == (0, -30) # pylint: disable=protected-access
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_background_shift_refuses_target_jumps(adapter:FakeAdapter) -> None:
    client = await make_client(WebAPI(adapter, WebAPIConfig(min_interval=0))) # no max_sv_step: default 20F
    try:
        assert (await client.post('/api/v1/background', json={'shift_temp': 25})).status == 422
        # moving the curve 30 s later lands before the background's CHARGE (seen in the simulator: 233 -> 348)
        adapter.targets = (233.0, 348.0)
        resp = await client.post('/api/v1/background', json={'shift_time': 30})
        assert resp.status == 422 and '233.0 to 348.0' in (await resp.json())['error']
        adapter.targets = (233.0, None)
        assert (await client.post('/api/v1/background', json={'shift_time': 30})).status == 422
        # small, smooth moves pass; temp and time combine
        adapter.targets = (233.0, 228.0)
        assert (await client.post('/api/v1/background', json={'shift_time': 10, 'shift_temp': 5})).status == 200
        adapter.targets = None # no current target (e.g. before CHARGE): only the temp limit applies
        assert (await client.post('/api/v1/background', json={'shift_time': 60})).status == 200
        assert adapter.bg['shift_time'] == 70
    finally:
        await client.close()


def test_app_adapter_background_targets(aw:Mock) -> None:
    aw.qmc.flagstart = True
    aw.qmc.timeindex = [1, 0, 0, 0, 0, 0, 0, 0]
    aw.qmc.timex = [0.0, 10.0, 20.0]
    aw.pidcontrol.svLookahead = 5
    aw.qmc.backgroundSmoothedBTat.side_effect = lambda t: {25.0: 300.0, -5.0: -1}.get(t, 250.0)
    a = AppAdapter(aw)
    assert a.background_targets(10) == (300.0, 250.0) # looks up 20 + 5 and 25 - 10
    assert a.background_targets(30) == (300.0, None) # before the background data starts
    aw.qmc.timeindex = [-1, 0, 0, 0, 0, 0, 0, 0]
    assert a.background_targets(10) is None # not charged


@pytest.mark.asyncio
async def test_sv_mode_switch_engages_override(adapter:FakeAdapter) -> None:
    api = WebAPI(adapter, WebAPIConfig(min_interval=0))
    client = await make_client(api)
    try:
        assert (await client.post('/api/v1/sv', json={'value': 350})).status == 200
        _age_writes(api)
        adapter.pid['mode'] = 'ramp_soak'
        state = await (await client.get('/api/v1/state')).json()
        assert state['control']['override'] is True and 'ramp_soak' in state['control']['override_reason']
        # release stops tracking the SV, which is no longer manual
        assert (await client.post('/api/v1/release')).status == 200
        assert api._control.expected_sv is None # pylint: disable=protected-access
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pid_on_off(adapter:FakeAdapter) -> None:
    client = await make_client(WebAPI(adapter, WebAPIConfig(min_interval=0)))
    try:
        assert (await client.post('/api/v1/pid', json={'on': 'yes'})).status == 400
        assert (await client.post('/api/v1/pid', json={'on': True})).status == 200
        assert (await client.post('/api/v1/pid', json={'on': False})).status == 200
        assert adapter.pid_calls == [True, False]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_pid_off_allowed_when_locked_on_refused(adapter:FakeAdapter) -> None:
    api = WebAPI(adapter, WebAPIConfig(min_interval=10))
    client = await make_client(api)
    try:
        assert (await client.post('/api/v1/pid', json={'on': True})).status == 200
        _age_writes(api)
        adapter.pid['active'] = False # user switched the PID off
        assert (await client.post('/api/v1/pid', json={'on': True})).status == 409
        state = await (await client.get('/api/v1/state')).json()
        assert 'PID switched off' in state['control']['override_reason']
        # off is the safe direction: accepted while locked and without rate limiting
        assert (await client.post('/api/v1/pid', json={'on': False})).status == 200
        assert (await client.post('/api/v1/pid', json={'on': False})).status == 200
        assert adapter.pid_calls == [True, False, False]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_watchdog_switches_pid_off_before_fallback(adapter:FakeAdapter) -> None:
    calls:list[str] = []
    orig_set_pid, orig_set_slider = adapter.set_pid, adapter.set_slider
    def set_pid(on:bool) -> None:
        calls.append(f'pid {on}')
        orig_set_pid(on)
    def set_slider(n:int, value:int) -> None:
        calls.append(f'slider {n}={value}')
        orig_set_slider(n, value)
    adapter.set_pid = set_pid # type: ignore[method-assign]
    adapter.set_slider = set_slider # type: ignore[method-assign]
    api = WebAPI(adapter, WebAPIConfig(min_interval=0, watchdog_timeout=5, watchdog_slider=3, watchdog_value=0, watchdog_pid_off=True))
    client = await make_client(api)
    try:
        assert (await client.post('/api/v1/pid', json={'on': True})).status == 200
        assert (await client.post('/api/v1/sv', json={'value': 400})).status == 200
        api._control.last_seen = time.monotonic() - 10 # pylint: disable=protected-access
        assert api.watchdog_tick() is True
        assert calls == ['pid True', 'pid False', 'slider 3=0']
        # the watchdog's own PID-off is not mistaken for a manual override
        _age_writes(api)
        state = await (await client.get('/api/v1/state')).json()
        assert state['control']['override'] is False and state['control']['watchdog_tripped'] is True
    finally:
        await client.close()


# ------------------------------------------------------------------ AppAdapter

@pytest.fixture
def aw() -> Mock:
    aw = Mock()
    aw.eventslidervalues = [10, 20, 30, 40]
    aw.eventslidermin = [0, 0, 0, 0]
    aw.eventslidermax = [100, 100, 9, 100]
    aw.eventslidervisibilities = [1, 1, 1, 0]
    aw.qmc.etypes = ['Air', 'Drum', 'Damper', 'Burner', '--']
    aw.qmc.mode = 'F'
    aw.qmc.flagon = True
    aw.qmc.flagstart = True
    aw.qmc.timex = [0.0, 1.0, 2.0, 3.0, 4.0]
    aw.qmc.temp1 = [400.0, 401.0, 402.0, 403.0, 404.0]
    aw.qmc.temp2 = [300.0, 250.0, 260.0, 270.0, 280.0]
    aw.qmc.timeindex = [1, 0, 3, 0, 0, 0, 0, 0] # CHARGE at idx 1, FCs at idx 3
    aw.qmc.rateofchange1 = 5.04
    aw.qmc.rateofchange2 = 12.36
    aw.qmc.background = False
    aw.qmc.device = 138
    aw.qmc.Controlbuttonflag = True
    aw.eventSliderStepSize.side_effect = lambda n: [5, 10, 1, 5][n]
    aw.eventquantifieractive = [1, 1, 0, 0]
    aw.eventquantifiercoarse = [2, 1, 1, 0]
    aw.pidcontrol.externalPIDControl.return_value = 4
    aw.pidcontrol.pidActive = True
    aw.pidcontrol.sv = 149.04
    aw.pidcontrol.svSliderMin = 0
    aw.pidcontrol.svSliderMax = 446
    aw.pidcontrol.svMode = 0
    aw.pidcontrol.pidSource = 1
    aw.pidcontrol.svLookahead = 4
    aw.qmc.timeB = [0.0, 1.0]
    aw.qmc.titleB = 'Colombia v1'
    aw.qmc.timeindexB = [0, 0, 0, 0, 0, 0, 0, 0]
    aw.qmc.backgroundprofile_moved_x = -10
    aw.qmc.backgroundprofile_moved_y = 5
    return aw


def test_app_adapter_snapshot(aw:Mock) -> None:
    s = AppAdapter(aw).snapshot()
    assert s['unit'] == 'F' and s['recording'] and s['charged']
    assert s['time'] == 3.0 # relative to CHARGE
    assert s['bt'] == 280.0 and s['et'] == 404.0
    assert s['bt_ror'] == 12.4
    assert s['events']['CHARGE'] == {'time': 0.0, 'bt': 250.0}
    assert s['events']['FCs'] == {'time': 2.0, 'bt': 270.0}
    assert s['events']['DRY'] is None
    assert s['sliders'][2] == {'slider': 2, 'name': 'Damper', 'value': 30, 'min': 0, 'max': 9, 'step': 1, 'visible': True}
    assert s['pid'] == {'available': True, 'active': True, 'sv': 149.0, 'sv_min': 0.0, 'sv_max': 446.0,
                        'mode': 'manual', 'source': 'BT', 'kind': 'kaleido', 'sv_tolerance': 3.0, 'lookahead': 4}
    assert s['background'] == {'loaded': False, 'title': None, 'charged': False, 'shift_temp': 5, 'shift_time': -10, 'unit': 'F'} # hidden
    aw.qmc.background = True
    assert AppAdapter(aw).snapshot()['background'] == {'loaded': True, 'title': 'Colombia v1', 'charged': True,
                                                       'shift_temp': 5, 'shift_time': -10, 'unit': 'F'}


def test_app_adapter_heat_slider(aw:Mock) -> None:
    a = AppAdapter(aw)
    aw.pidcontrol.pidPositiveTarget = 4
    assert a.heat_slider() == 3
    aw.pidcontrol.pidPositiveTarget = 0
    assert a.heat_slider() is None


def test_app_adapter_slider_step_and_tolerance(aw:Mock) -> None:
    a = AppAdapter(aw)
    assert [a.slider_step(n) for n in range(4)] == [5, 10, 1, 5]
    # tolerance follows the quantifier step, only where a quantifier is active
    assert [a.slider_tolerance(n) for n in range(4)] == [5, 10, 1, 1]


def test_app_adapter_pid_availability(aw:Mock) -> None:
    a = AppAdapter(aw)
    aw.qmc.Controlbuttonflag = False
    assert a.pid_info()['available'] is False # Kaleido PID needs the Control button
    aw.pidcontrol.externalPIDControl.return_value = 1
    assert a.pid_info()['available'] is True and a.pid_info()['kind'] == 'modbus' # MODBUS PID does not
    aw.qmc.device = 0
    assert a.pid_info()['available'] is False # Fuji not supported
    aw.pidcontrol.svMode = 2
    aw.pidcontrol.sv = None
    assert a.pid_info()['mode'] == 'background' and a.pid_info()['sv'] is None
    assert a.pid_info()['sv_tolerance'] == 0.5 # only the Kaleido gets the whole-degree-C allowance


def test_app_adapter_kaleido_sv_tolerance_celsius(aw:Mock) -> None:
    aw.qmc.mode = 'C'
    assert AppAdapter(aw).pid_info()['sv_tolerance'] == 1.5


def test_app_adapter_snapshot_monitoring_only(aw:Mock) -> None:
    aw.qmc.flagstart = False
    aw.qmc.on_timex = [10.0, 11.0, 12.0]
    aw.qmc.on_temp1 = [400.0, 401.0, 402.0]
    aw.qmc.on_temp2 = [300.0, 301.0, 302.0]
    s = AppAdapter(aw).snapshot()
    assert not s['recording'] and not s['charged']
    assert s['time'] is None # rolling monitor buffer has no meaningful roast time
    assert s['bt'] == 302.0 and s['et'] == 402.0
    assert s['events'] == {}


def test_app_adapter_background_is_charge_relative(aw:Mock) -> None:
    aw.qmc.background = True
    aw.qmc.backgroundSmoothedBTat.return_value = 222.04
    s = AppAdapter(aw).snapshot()
    assert s['bt_background'] == 222.0
    aw.qmc.backgroundSmoothedBTat.assert_called_once_with(3.0, relative=True)


def test_app_adapter_writes(aw:Mock) -> None:
    a = AppAdapter(aw)
    a.set_slider(3, 55)
    aw.addEventSignal.emit.assert_called_once_with(55, 3, True, True, False)
    a.mark_event('DROP')
    aw.qmc.markDropSignal.emit.assert_called_once_with(False)
    a.set_sv(380)
    aw.setSVSignal.emit.assert_called_once_with(380.0)
    a.set_pid(True)
    aw.pidOnSignal.emit.assert_called_once_with()
    a.set_pid(False)
    aw.pidOffSignal.emit.assert_called_once_with()
    a.set_pid_mode('background')
    aw.pidModeSignal.emit.assert_called_once_with(2)
    a.set_pid_lookahead(6)
    aw.pidLookaheadSignal.emit.assert_called_once_with(6)
    a.shift_background(-5, 30)
    assert aw.qmc.moveBackgroundSignal.emit.call_args_list == [(('down', 5),), (('right', 30),)]
