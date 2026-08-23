"""Config flow tests.

These exercise the flow through Home Assistant itself rather than by calling the
methods directly, so they cover the wiring as well as the logic.
"""

from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType

from custom_components.beurer_freshhome.api import (
    BeurerAuthError,
    BeurerClientSecretError,
    BeurerConnectionError,
)
from custom_components.beurer_freshhome.const import (
    DOMAIN,
)

CREDENTIALS = {CONF_EMAIL: "user@example.com", CONF_PASSWORD: "hunter2"}
DEVICE = {"id": "0000000000000001", "name": "Air purifier", "model": "LR500"}

LIST_DEVICES = "custom_components.beurer_freshhome.api.BeurerClient.async_list_devices"


async def _start(hass):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def test_user_flow_creates_entry(hass):
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM

    with (
        patch(LIST_DEVICES, return_value=[DEVICE]),
        patch(
            "custom_components.beurer_freshhome.async_setup_entry", return_value=True
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "user@example.com"
    assert result["data"] == CREDENTIALS


@pytest.mark.parametrize(
    ("side_effect", "expected"),
    [
        (BeurerAuthError("nope"), "invalid_auth"),
        (BeurerConnectionError("down"), "cannot_connect"),
        # The distinction that matters: a stale app secret must not be reported as
        # a bad password, or every user goes and resets a password that was fine.
        (BeurerClientSecretError("rotated"), "invalid_client_secret"),
    ],
)
async def test_user_flow_errors(hass, side_effect, expected):
    result = await _start(hass)
    with patch(LIST_DEVICES, side_effect=side_effect):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": expected}


async def test_user_flow_no_devices(hass):
    result = await _start(hass)
    with patch(LIST_DEVICES, return_value=[]):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )

    assert result["errors"] == {"base": "no_devices"}


async def test_recovers_after_an_error(hass):
    """A failed attempt must leave the form usable, not wedge the flow."""
    result = await _start(hass)
    with patch(LIST_DEVICES, side_effect=BeurerAuthError("nope")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )
    assert result["errors"]

    with (
        patch(LIST_DEVICES, return_value=[DEVICE]),
        patch(
            "custom_components.beurer_freshhome.async_setup_entry", return_value=True
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_duplicate_account_aborts(hass):
    entry = config_entries.ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title="user@example.com",
        data=CREDENTIALS,
        source=config_entries.SOURCE_USER,
        unique_id="user@example.com",
        options={},
        discovery_keys={},
        subentries_data=(),
    )
    entry.add_to_hass(hass)

    result = await _start(hass)
    with patch(LIST_DEVICES, return_value=[DEVICE]):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], CREDENTIALS
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
