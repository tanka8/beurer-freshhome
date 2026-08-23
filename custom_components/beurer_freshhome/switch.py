"""Switch entities."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BeurerConfigEntry
from .const import FN_SLEEP, FN_UV
from .entity import BeurerEntity

# Commands all travel over the one shared WebSocket and are cheap, so there is no
# reason to serialise them. Nothing here polls.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BeurerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    # Auto mode is not a switch here - it is an option on the mode select, since on
    # these devices auto is a way of choosing the speed.
    entities: list[BeurerEntity] = []
    for coordinator in entry.runtime_data.coordinators:
        # Only offer what this model actually reports. A humidifier, say, has no
        # UV lamp and should not gain a switch that does nothing.
        if coordinator.supports("uv"):
            entities.append(BeurerUVSwitch(coordinator))
        if coordinator.supports("sleep"):
            entities.append(BeurerNightModeSwitch(coordinator))
    async_add_entities(entities)


class _BeurerToggle(BeurerEntity, SwitchEntity):
    """A status field that maps to a single 0/1 command."""

    _function: str
    _status_key: str

    @property
    def is_on(self) -> bool:
        return bool(self.status.get(self._status_key))

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command(self._function, 1)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_send_command(self._function, 0)


class BeurerUVSwitch(_BeurerToggle):
    """The UV-C lamp."""

    _attr_translation_key = "uv"
    _function = FN_UV
    _status_key = "uv"

    def __init__(self, coordinator):
        super().__init__(coordinator, "uv")


class BeurerNightModeSwitch(_BeurerToggle):
    """Night mode - independent of auto, and its own toggle in the app.

    Believed to be primarily the display-off setting; whether it also caps the fan
    speed is unconfirmed.
    """

    _attr_translation_key = "night_mode"
    _function = FN_SLEEP
    _status_key = "sleep"

    def __init__(self, coordinator):
        super().__init__(coordinator, "night_mode")
