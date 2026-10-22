"""The coordinator's settings write, through Home Assistant's own aiohttp session."""

from unittest.mock import MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.beurer_freshhome.api import BeurerAuth, BeurerClient
from custom_components.beurer_freshhome.const import BORDER_VALUES_URL, TOKEN_URL
from custom_components.beurer_freshhome.coordinator import BeurerCoordinator

DEVICE = {"id": "0000000000000001", "name": "Air purifier", "model": "LR500"}

# What the coordinator read when the entry was set up...
STALE = {
    "deviceId": DEVICE["id"],
    "deviceLocation": "manual",
    "devicePmSensitivity": "standard",
    "humidMin": 40,
    "humidMax": 60,
    "tempMin": 18,
    "tempMax": 25,
}
# ...and what the server holds after the comfort ranges were edited in the app.
FRESH = {**STALE, "humidMin": 35, "tempMax": 28}

TOKEN = {"access_token": "access", "refresh_token": "refresh", "expires_in": 3600}


@pytest.fixture
async def coordinator(hass, aioclient_mock):
    aioclient_mock.post(TOKEN_URL, json=TOKEN)
    session = async_get_clientsession(hass)
    auth = BeurerAuth(session, "user@example.com", "hunter2")
    coordinator = BeurerCoordinator(
        hass, MagicMock(), BeurerClient(session, auth), DEVICE
    )
    yield coordinator
    await coordinator.async_shutdown()


def _border_value_writes(aioclient_mock):
    return [
        call
        for call in aioclient_mock.mock_calls
        if call[0].upper() == "POST" and str(call[1]).startswith(BORDER_VALUES_URL)
    ]


async def test_sensitivity_write_keeps_what_changed_since_the_last_read(
    coordinator, aioclient_mock
):
    """The write must carry the server's current ranges, not the cached ones."""
    coordinator.border_values = STALE
    aioclient_mock.get(BORDER_VALUES_URL, json=FRESH)
    aioclient_mock.post(BORDER_VALUES_URL, json={})

    await coordinator.async_set_sensitivity("moderate")

    writes = _border_value_writes(aioclient_mock)
    assert len(writes) == 1
    assert writes[0][2] == {**FRESH, "devicePmSensitivity": "moderate"}


async def test_sensitivity_write_is_refused_when_settings_cannot_be_read(
    coordinator, aioclient_mock
):
    """No read means no write, and the user is told rather than left guessing."""
    coordinator.border_values = STALE
    aioclient_mock.get(BORDER_VALUES_URL, status=500)
    aioclient_mock.post(BORDER_VALUES_URL, json={})

    with pytest.raises(HomeAssistantError):
        await coordinator.async_set_sensitivity("moderate")

    assert not _border_value_writes(aioclient_mock)
