"""The account email and the device id must not reach a log line or a message.

Beurer's API takes the account email in the query string of the device list
(?email=) and the device id, which embeds the device's MAC address, in the query
string of the settings (?deviceId=). aiohttp puts the full URL in the text of the
error that raise_for_status() raises, so interpolating that error into a message
would carry both into Home Assistant's log and onto the screen.
"""

from __future__ import annotations

import asyncio
import logging

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from tests.loader import load_module

api = load_module("api")
const = load_module("const")

EMAIL = "user.name@example.com"
DEVICE_ID = "0123456789AB-device"

DEVICES_URL = URL(const.DEVICES_URL).with_query({"email": EMAIL})
SETTINGS_URL = URL(const.BORDER_VALUES_URL).with_query({"deviceId": DEVICE_ID})
UPGRADE_URL = URL(const.HUB_URL).with_query({"id": "connection-token"})

# Whatever form the URL takes in a message, one of these is in it.
LEAKS = (
    "user.name",
    "example.com",
    "email=",
    DEVICE_ID,
    "deviceId=",
    "connection-token",
)


def http_error(url: URL, status: int = 500) -> aiohttp.ClientResponseError:
    """The error aiohttp raises for a failed request, URL and all."""
    headers = CIMultiDictProxy(CIMultiDict())
    request_info = aiohttp.RequestInfo(url, "GET", headers, url)
    return aiohttp.ClientResponseError(
        request_info, (), status=status, message="Internal Server Error"
    )


def assert_clean(text: str) -> None:
    for leak in LEAKS:
        assert leak not in text, f"{leak!r} leaked into: {text}"


def test_the_fixture_error_really_does_carry_the_url():
    """Otherwise every test below would pass for the wrong reason."""
    text = str(http_error(DEVICES_URL))
    assert "email=" in text
    assert "user.name" in text


class FakeAuth:
    async def async_token(self) -> str:
        return "token"


class Reply:
    """A response that fails, or - for a read that has to work - succeeds."""

    def __init__(self, url: URL, ok: bool = False):
        self.status = 200 if ok else 500
        self._url = url
        self._ok = ok

    def raise_for_status(self) -> None:
        if not self._ok:
            raise http_error(self._url)

    async def json(self):
        return {"deviceId": DEVICE_ID, "devicePmSensitivity": "standard"}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class Session:
    """Requests fail with an HTTP error naming the URL that was requested."""

    def __init__(self, reads_work: bool = False):
        self._reads_work = reads_work

    def get(self, url, params=None, headers=None):
        return Reply(URL(url).with_query(params or {}), ok=self._reads_work)

    def post(self, url, json=None, headers=None):
        return Reply(URL(url))


def test_describe_drops_the_url_from_an_http_error():
    assert api._describe(http_error(DEVICES_URL)) == "HTTP 500 Internal Server Error"


def test_describe_drops_a_query_from_any_other_error_text():
    text = api._describe(OSError(f"could not reach {SETTINGS_URL} just now"))
    assert_clean(text)
    assert const.BORDER_VALUES_URL in text, "the URL itself is harmless"


def test_describe_leaves_ordinary_text_alone():
    assert api._describe(OSError("Why? Because.")) == "Why? Because."


@pytest.mark.parametrize(
    ("reads_work", "call"),
    [
        pytest.param(False, lambda c: c.async_list_devices(EMAIL), id="list-devices"),
        pytest.param(
            False, lambda c: c.async_get_border_values(DEVICE_ID), id="read-settings"
        ),
        # The write reads first; here the read works and the write itself fails.
        pytest.param(
            True,
            lambda c: c.async_set_border_values(DEVICE_ID, tempMax=30),
            id="write-settings",
        ),
    ],
)
def test_a_failed_request_is_reported_without_its_url(reads_work, call):
    client = api.BeurerClient(Session(reads_work), FakeAuth())

    with pytest.raises(api.BeurerConnectionError) as raised:
        asyncio.run(call(client))

    assert_clean(str(raised.value))
    assert "HTTP 500" in str(raised.value), "the status is still reported"


class ClosableSession:
    closed = False


def test_the_hubs_log_lines_do_not_carry_the_upgrade_url(monkeypatch, caplog):
    """A failed WebSocket upgrade is the other place a URL with an id appears."""
    caplog.set_level(logging.DEBUG)
    hub = api.BeurerHub(session=ClosableSession(), auth=None)

    async def upgrade_refused():
        raise http_error(UPGRADE_URL, status=502)

    hub._connect_and_pump = upgrade_refused
    real_sleep = asyncio.sleep
    rounds = []

    async def stop_after_two(seconds, *args, **kwargs):
        rounds.append(seconds)
        if len(rounds) >= 2:  # by then: one warning, then one debug line
            hub._session.closed = True
        await real_sleep(0)

    monkeypatch.setattr(api.asyncio, "sleep", stop_after_two)
    asyncio.run(hub._run())

    assert "Beurer hub connection lost" in caplog.text
    assert "Beurer hub still unreachable" in caplog.text
    assert "HTTP 502" in caplog.text
    assert_clean(caplog.text)
