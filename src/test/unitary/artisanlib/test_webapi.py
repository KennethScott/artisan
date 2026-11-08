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

from artisanlib.webapi import WebAPI, WebAPIConfig, AppAdapter, MANUAL_OVERRIDE_GRACE


class FakeAdapter:
    def __init__(self) -> None:
        self.values = [0, 0, 0, 0]
        self.limits = [(0, 100)] * 4
        self.recording = True
        self.timeindex = [0, 0, 0, 0, 0, 0, 0, 0]
        self.set_calls: list[tuple[int, int]] = []
        self.events: list[str] = []

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
    def set_slider(self, n:int, value:int) -> None:
        self.set_calls.append((n, value))
        self.values[n] = value # the real adapter moves the slider asynchronously in the GUI thread
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
    assert s['sliders'][2] == {'slider': 2, 'name': 'Damper', 'value': 30, 'min': 0, 'max': 9, 'visible': True}


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
