"""Fan entity - the purifier itself."""

from __future__ import annotations

from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.percentage import (
    ordered_list_item_to_percentage,
    percentage_to_ordered_list_item,
)

from . import BeurerConfigEntry
from .const import (
    FN_FAN,
    FN_MODE,
    FN_POWER,
    MODE_AUTO,
    MODE_MANUAL,
    MODES,
    fan_speed_names,
    fan_speeds,
)
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
        BeurerFan(coordinator) for coordinator in entry.runtime_data.coordinators
    )


class BeurerFan(BeurerEntity, FanEntity):
    """The air purifier as a fan entity."""

    _attr_name = None  # the device name is the entity name
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.PRESET_MODE
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )
    _attr_preset_modes = MODES

    def __init__(self, coordinator):
        super().__init__(coordinator, "fan")
        self._speeds = fan_speeds(coordinator.model)
        self._speed_names = fan_speed_names(coordinator.model)
        self._attr_speed_count = len(self._speeds)

    @property
    def is_on(self) -> bool:
        return bool(self.status.get("power"))

    @property
    def percentage(self) -> int:
        """Fan speed as a percentage.

        The device reports fan 0 while powered off; that is not a settable speed,
        so it maps to 0% rather than to the lowest step.
        """
        if not self.is_on:
            return 0
        speed = self.status.get("fan")
        if speed not in self._speeds:
            return 0
        return ordered_list_item_to_percentage(self._speeds, speed)

    @property
    def preset_mode(self) -> str:
        return MODE_AUTO if self.status.get("mode") else MODE_MANUAL

    @property
    def extra_state_attributes(self) -> dict:
        """The app's label for the running speed, where the top one is Turbo."""
        return {"speed_name": self._speed_names.get(self.status.get("fan"))}

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        await self._send(FN_POWER, 1)
        if preset_mode is not None:
            await self.async_set_preset_mode(preset_mode)
        if percentage is not None:
            await self.async_set_percentage(percentage)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send(FN_POWER, 0)

    async def async_set_percentage(self, percentage: int) -> None:
        if percentage == 0:
            await self.async_turn_off()
            return

        # Auto picks the speed from the particle count, so an explicit speed has to
        # leave auto or it gets overridden straight back.
        #
        # Night mode is deliberately left alone. It appears to be mainly a display
        # toggle and may or may not cap the speed; cancelling it here would turn the
        # user's display back on as a side effect of changing speed.
        if self.status.get("mode"):
            await self._send(FN_MODE, 0)

        await self._send(
            FN_FAN, percentage_to_ordered_list_item(self._speeds, percentage)
        )

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        await self._send(FN_MODE, 1 if preset_mode == MODE_AUTO else 0)

    async def _send(self, function: str, value: int) -> None:
        await self.coordinator.async_send_command(function, value)
