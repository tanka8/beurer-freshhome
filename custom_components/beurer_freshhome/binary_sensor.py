"""Binary sensor entities."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BeurerConfigEntry
from .entity import BeurerEntity

# Commands all travel over the one shared WebSocket and are cheap, so there is no
# reason to serialise them. Nothing here polls.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BeurerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities(
        BeurerFilterProblem(coordinator)
        for coordinator in entry.runtime_data.coordinators
        if coordinator.supports("filterReplace")
    )


class BeurerFilterProblem(BeurerEntity, BinarySensorEntity):
    """On when the device is asking for a filter change."""

    _attr_translation_key = "filter_replace"
    _attr_device_class = BinarySensorDeviceClass.PROBLEM

    def __init__(self, coordinator):
        super().__init__(coordinator, "filter_replace")

    @property
    def is_on(self) -> bool:
        return bool(self.status.get("filterReplace"))
