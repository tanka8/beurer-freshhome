"""borderValues writes: merge into a fresh read, never into a stale one.

The endpoint replaces the whole record, so a write has to send every field back.
Building that body from an old read puts the old value of everything back, which
is how a comfort range edited in the Beurer app would silently be undone by
changing the auto sensitivity from Home Assistant.

The fake below behaves like the real endpoint in the one way that matters here: a
POST replaces the record with exactly what it was sent.
"""

from __future__ import annotations

import asyncio
import copy

import aiohttp
import pytest

from tests.loader import load_module

api = load_module("api")

DEVICE_ID = "0000000000000001"

RECORD = {
    "deviceId": DEVICE_ID,
    "deviceLocation": "manual",
    "devicePmSensitivity": "standard",
    "humidMin": 40,
    "humidMax": 60,
    "tempMin": 18,
    "tempMax": 25,
}


class FakeAuth:
    async def async_token(self) -> str:
        return "token"


class FakeResponse:
    def __init__(self, status: int = 200, payload=None):
        self.status = status
        self._payload = payload

    async def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientError(f"HTTP {self.status}")


class _Exchange:
    """What session.get(...) and session.post(...) return: an async context."""

    def __init__(self, respond):
        self._respond = respond

    async def __aenter__(self) -> FakeResponse:
        return await self._respond()

    async def __aexit__(self, *exc) -> bool:
        return False


class FakeServer:
    """The borderValues endpoint, standing in for the aiohttp session."""

    def __init__(self, record: dict | None = None):
        self.record = copy.deepcopy(RECORD if record is None else record)
        self.calls: list[str] = []
        self.posts: list[dict] = []
        self.get_status = 200
        # When set, served instead of the record (to model a broken response).
        self.get_payload: object = None

    def get(self, url, params=None, headers=None):
        async def respond() -> FakeResponse:
            # Yielding here is what lets a second, concurrent call interleave.
            await asyncio.sleep(0)
            self.calls.append("GET")
            if self.get_status != 200:
                return FakeResponse(self.get_status)
            if self.get_payload is not None:
                return FakeResponse(200, self.get_payload)
            return FakeResponse(200, copy.deepcopy(self.record))

        return _Exchange(respond)

    def post(self, url, json=None, headers=None):
        async def respond() -> FakeResponse:
            await asyncio.sleep(0)
            self.calls.append("POST")
            self.posts.append(copy.deepcopy(json))
            # Replace-the-whole-record: only what was sent survives.
            self.record = copy.deepcopy(json)
            return FakeResponse(200)

        return _Exchange(respond)


def _client(server: FakeServer):
    return api.BeurerClient(server, FakeAuth())


def _set(server: FakeServer, **changes) -> None:
    asyncio.run(_client(server).async_set_border_values(DEVICE_ID, **changes))


def test_a_write_does_not_revert_a_change_made_since_the_last_read():
    """The audit's BF-03 scenario.

    The coordinator reads the record; the user then changes the comfort ranges in
    the Beurer app; the coordinator is later asked to change the sensitivity. Only
    the sensitivity may differ afterwards - the ranges must keep the app's values.
    """
    server = FakeServer()
    client = _client(server)

    async def run():
        # The coordinator's read, which is what the write used to merge into.
        stale = await client.async_get_border_values(DEVICE_ID)
        # Changed in the Beurer app afterwards.
        server.record["humidMin"] = 35
        server.record["tempMax"] = 28
        await client.async_set_border_values(DEVICE_ID, devicePmSensitivity="moderate")
        return stale

    stale = asyncio.run(run())

    assert stale["humidMin"] == 40, "the earlier read really was out of date"
    assert server.record == {
        **RECORD,
        "humidMin": 35,
        "tempMax": 28,
        "devicePmSensitivity": "moderate",
    }


def test_a_write_reads_immediately_before_it_writes():
    server = FakeServer()
    _set(server, devicePmSensitivity="moderate")
    assert server.calls == ["GET", "POST"]


def test_the_requested_change_wins_over_the_fresh_value():
    server = FakeServer()
    _set(server, tempMax=30)
    assert server.record["tempMax"] == 30
    assert server.record["devicePmSensitivity"] == "standard"


def test_the_device_id_falls_back_to_the_one_asked_for():
    """A record without deviceId must not produce a write without one."""
    server = FakeServer({k: v for k, v in RECORD.items() if k != "deviceId"})
    _set(server, tempMax=30)
    assert server.record["deviceId"] == DEVICE_ID


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param({"get_status": 500}, id="read-fails"),
        pytest.param({"get_payload": {}}, id="read-empty"),
        pytest.param({"get_payload": []}, id="read-not-an-object"),
    ],
)
def test_nothing_is_written_when_the_current_settings_cannot_be_read(broken):
    """Writing without a read would blank every field that is not being changed."""
    server = FakeServer()
    for name, value in broken.items():
        setattr(server, name, value)

    with pytest.raises(api.BeurerError):
        _set(server, devicePmSensitivity="moderate")

    assert server.posts == []


def test_a_rejected_read_is_an_auth_problem():
    server = FakeServer()
    server.get_status = 401
    with pytest.raises(api.BeurerAuthError):
        _set(server, devicePmSensitivity="moderate")
    assert server.posts == []


def test_concurrent_writes_do_not_revert_each_other():
    """Both must land.

    Without the lock each would read the record before the other had written it,
    and whichever wrote second would put the first one back.
    """
    server = FakeServer()
    client = _client(server)

    async def run():
        await asyncio.gather(
            client.async_set_border_values(DEVICE_ID, devicePmSensitivity="moderate"),
            client.async_set_border_values(DEVICE_ID, tempMax=30),
        )

    asyncio.run(run())

    assert server.record["devicePmSensitivity"] == "moderate"
    assert server.record["tempMax"] == 30
    assert server.calls == ["GET", "POST", "GET", "POST"]
