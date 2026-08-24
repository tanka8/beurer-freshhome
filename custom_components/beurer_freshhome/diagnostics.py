"""Diagnostics.

The single most useful thing in a bug report from an untested model is its raw
status frame, since that is what decides which entities exist and how the values
decode. This dumps it with the identifying and personal fields removed.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant

from . import BeurerConfigEntry

REDACT_CONFIG = {CONF_EMAIL, CONF_PASSWORD}
# deviceId embeds the device's MAC address.
REDACT_DEVICE = {"id", "deviceId"}

# The device record comes from /api/users/list, and its full field set is not known
# - only an LR-500 on one account has ever been seen. Since these downloads are
# meant to be pasted into public issues, the record is filtered to the fields that
# are actually useful for adding model support, rather than redacting the two
# identifying fields that happen to be known and hoping there are no others.
DEVICE_KEYS = ("model", "name", "type", "deviceType", "firmwareVersion", "swVersion")


def _device_summary(device: dict) -> dict:
    """Only the fields needed to add support for a model."""
    return {key: device[key] for key in DEVICE_KEYS if key in device}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: BeurerConfigEntry
) -> dict[str, Any]:
    data = entry.runtime_data
    return {
        "config": async_redact_data(dict(entry.data), REDACT_CONFIG),
        "hub_connected": data.hub.connected,
        "devices": [
            {
                "model": coordinator.model,
                "available": coordinator.available,
                "device": _device_summary(coordinator.device),
                # The raw frame, verbatim apart from the id - this is the bit worth
                # pasting into an issue for an unsupported model.
                "status": async_redact_data(coordinator.data or {}, REDACT_DEVICE),
                "settings": async_redact_data(coordinator.border_values, REDACT_DEVICE),
            }
            for coordinator in data.coordinators
        ],
    }
