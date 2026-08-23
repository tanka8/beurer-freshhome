"""Select entities.

Mode and fan speed are deliberately kept apart, because the app treats them as two
separate things and because merging them hides information: in auto the device still
picks a numbered speed, so a combined control would only ever read "auto".
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BeurerConfigEntry
from .const import (
    FN_FAN,
    FN_MODE,
    MODE_AUTO,
    MODES,
    PM_SENSITIVITIES,
    fan_speed_names,
)
from .entity import BeurerEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BeurerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entities: list[BeurerEntity] = []
    for coordinator in entry.runtime_data.coordinators:
        if coordinator.supports("mode"):
            entities.append(BeurerModeSelect(coordinator))
        if coordinator.supports("fan"):
            entities.append(BeurerFanSpeedSelect(coordinator))
        # REST-backed, so it depends on that call having succeeded rather than on
        # anything in the status frame.
        if coordinator.border_values.get("devicePmSensitivity") is not None:
            entities.append(BeurerSensitivitySelect(coordinator))
    async_add_entities(entities)


class BeurerModeSelect(BeurerEntity, SelectEntity):
    """Auto or manual."""

    _attr_translation_key = "mode"
    _attr_icon = "mdi:auto-mode"
    _attr_options = MODES

    def __init__(self, coordinator):
        super().__init__(coordinator, "mode")

    @property
    def current_option(self) -> str | None:
        if not self.status:
            return None
        return MODE_AUTO if self.status.get("mode") else "manual"

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.hub.async_send_command(
            self.coordinator.device_id, FN_MODE, 1 if option == MODE_AUTO else 0
        )


class BeurerFanSpeedSelect(BeurerEntity, SelectEntity):
    """The numbered speeds. Reports what is running, including auto's choice."""

    _attr_translation_key = "fan_speed"
    _attr_icon = "mdi:fan"

    def __init__(self, coordinator):
        super().__init__(coordinator, "fan_speed")
        self._speed_names = fan_speed_names(coordinator.model)
        self._name_to_speed = {v: k for k, v in self._speed_names.items()}
        self._attr_options = list(self._speed_names.values())

    @property
    def current_option(self) -> str | None:
        # fan is 0 while powered off, which is not one of the options.
        return self._speed_names.get(self.status.get("fan"))

    async def async_select_option(self, option: str) -> None:
        speed = self._name_to_speed.get(option)
        if speed is None:
            return
        device_id = self.coordinator.device_id
        hub = self.coordinator.hub

        # Choosing a speed by hand means leaving auto, or auto overrides it again.
        if self.status.get("mode"):
            await hub.async_send_command(device_id, FN_MODE, 0)
        await hub.async_send_command(device_id, FN_FAN, speed)


class BeurerSensitivitySelect(BeurerEntity, SelectEntity):
    """Auto-mode particle sensitivity.

    Unlike everything else here this lives in REST, not the status push, so it is
    read at setup and re-read after each write.
    """

    _attr_translation_key = "auto_sensitivity"
    _attr_icon = "mdi:tune"
    _attr_options = PM_SENSITIVITIES
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator):
        super().__init__(coordinator, "auto_sensitivity")

    @property
    def available(self) -> bool:
        return super().available and bool(self.coordinator.border_values)

    @property
    def current_option(self) -> str | None:
        # The app writes "moderate " with a trailing space; strip defensively in
        # case the server ever echoes back what it was given.
        value = self.coordinator.border_values.get("devicePmSensitivity")
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value if value in PM_SENSITIVITIES else None

    async def async_select_option(self, option: str) -> None:
        await self.coordinator.async_set_sensitivity(option)
