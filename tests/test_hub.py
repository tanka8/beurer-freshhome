"""Replay captured SignalR frames through the real hub.

No credentials, no network: the frames recorded from a live session are fed into
BeurerHub._handle_text and the dispatch is checked. This is the regression test for
the protocol decoding, and for the per-account dispatch that replaced the old
one-socket-per-device design.

    python -m pytest tests/
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tests.loader import load_module

api = load_module("api")
const = load_module("const")

FIXTURE = Path(__file__).parent / "fixtures" / "hub_frames.jsonl"
DEVICE_ID = "0000000000000001"
OTHER_DEVICE = "0000000000000002"


class FakeWS:
    """Records what the hub sends back."""

    def __init__(self):
        self.sent: list[str] = []

    async def send_str(self, data: str) -> None:
        self.sent.append(data)


def frames() -> list[str]:
    """Raw wire text of each captured server frame."""
    return [
        line + const.RECORD_SEPARATOR
        for line in FIXTURE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def replay(hub, ws) -> None:
    for raw in frames():
        await hub._handle_text(raw, ws)


@pytest.fixture
def hub():
    return api.BeurerHub(session=None, auth=None)


def test_fixture_present():
    assert FIXTURE.exists(), "capture fixture is missing"
    assert len(frames()) > 100


def test_status_frames_decode(hub):
    seen: list[dict] = []
    hub.register(DEVICE_ID, seen.append)
    asyncio.run(replay(hub, FakeWS()))

    assert seen, "no status frames decoded"
    required = {"deviceId", "power", "fan", "pm", "humidity", "temperature", "uv"}
    for status in seen:
        assert required <= status.keys()


def test_command_echoes_are_not_treated_as_status(hub):
    """The server echoes our own commands back on the same channel.

    Treating those as state would produce phantom updates, so only frames whose
    function is "status" may reach a listener.
    """
    seen: list[dict] = []
    hub.register(DEVICE_ID, seen.append)
    asyncio.run(replay(hub, FakeWS()))

    assert not [s for s in seen if s.get("type") == "cmd"]
    assert all(s.get("function") == "status" for s in seen)


def test_pings_are_answered(hub):
    """SignalR drops the connection if its keepalive goes unanswered."""
    ws = FakeWS()
    asyncio.run(replay(hub, ws))

    assert ws.sent, "no ping response sent"
    for msg in ws.sent:
        assert json.loads(msg.rstrip(const.RECORD_SEPARATOR))["type"] == const.MSG_PING


def test_dispatch_is_per_device(hub):
    """One socket serves the whole account, so frames must be routed by device id.

    A listener registered for a different device must receive nothing.
    """
    mine: list[dict] = []
    theirs: list[dict] = []
    hub.register(DEVICE_ID, mine.append)
    hub.register(OTHER_DEVICE, theirs.append)
    asyncio.run(replay(hub, FakeWS()))

    assert mine
    assert not theirs, "frames leaked to another device's listener"


def test_multiple_listeners_for_one_device(hub):
    a: list[dict] = []
    b: list[dict] = []
    hub.register(DEVICE_ID, a.append)
    hub.register(DEVICE_ID, b.append)
    asyncio.run(replay(hub, FakeWS()))

    assert len(a) == len(b) > 0


def test_unregister_stops_delivery(hub):
    seen: list[dict] = []
    unregister = hub.register(DEVICE_ID, seen.append)
    unregister()
    asyncio.run(replay(hub, FakeWS()))

    assert not seen


def test_decoded_values_are_in_range(hub):
    seen: list[dict] = []
    hub.register(DEVICE_ID, seen.append)
    asyncio.run(replay(hub, FakeWS()))

    assert {s["power"] for s in seen} <= {0, 1}
    assert {s["fan"] for s in seen} <= {0, 1, 2, 3, 4, 5}
    assert {s["airquality"] for s in seen} <= {1, 2, 3, 4}


def test_malformed_frames_are_ignored(hub):
    """Junk on the wire must not take the connection down."""
    seen: list[dict] = []
    hub.register(DEVICE_ID, seen.append)
    ws = FakeWS()

    async def run():
        await hub._handle_text("not json" + const.RECORD_SEPARATOR, ws)
        await hub._handle_text("{}" + const.RECORD_SEPARATOR, ws)
        await hub._handle_text("", ws)
        await hub._handle_text(
            json.dumps({"target": "ReceiveMessage", "arguments": ["nope"]})
            + const.RECORD_SEPARATOR,
            ws,
        )

    asyncio.run(run())
    assert not seen


class _FakeSession:
    closed = False


def test_auth_failure_stops_retrying_and_asks_for_reauth():
    """Bad credentials cannot be fixed by retrying.

    The hub must hand the failure up so Home Assistant can prompt the user, and
    must stop. Regressing this would leave it looping forever against a password
    that will never work, so the wait_for guards against a hang rather than
    letting the suite stall.
    """
    hub = api.BeurerHub(session=_FakeSession(), auth=None)
    asked = []
    hub.on_auth_error = lambda: asked.append(True)

    async def always_rejected():
        raise api.BeurerAuthError("credentials rejected")

    hub._connect_and_pump = always_rejected

    async def run():
        await asyncio.wait_for(hub._run(), timeout=2)

    asyncio.run(run())
    assert asked == [True], "reauth was not requested"


def test_a_closed_session_stops_the_loop():
    """Unloading the entry closes the session; retrying then is pointless."""
    session = _FakeSession()
    session.closed = True
    hub = api.BeurerHub(session=session, auth=None)

    async def boom():
        raise api.BeurerConnectionError("gone")

    hub._connect_and_pump = boom

    async def run():
        await asyncio.wait_for(hub._run(), timeout=2)

    asyncio.run(run())
