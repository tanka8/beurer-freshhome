"""Shared entity base."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import BeurerCoordinator


class BeurerEntity(CoordinatorEntity[BeurerCoordinator]):
    """Base for every Beurer entity."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: BeurerCoordinator, key: str):
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.device_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.device_id)},
            manufacturer="Beurer",
            model=coordinator.device.get("model"),
            name=coordinator.device.get("name", "Air purifier"),
            # deviceLocation is deliberately NOT used as the area. Despite the name
            # it is the comfort-profile preset that goes with the temperature and
            # humidity ranges, and it flips to "manual" as soon as any of them is
            # customised - it is not a room.
        )

    @property
    def available(self) -> bool:
        return self.coordinator.available

    @property
    def status(self) -> dict:
        """The most recent status frame, or an empty dict before the first one."""
        return self.coordinator.data or {}
