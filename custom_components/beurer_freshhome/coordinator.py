"""Per-device coordinator, fed by the shared account hub."""

from __future__ import annotations

import asyncio
import logging
import time

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import BeurerClient, BeurerError, BeurerHub
from .const import DOMAIN, FIRST_STATUS_TIMEOUT, STALE_AFTER

_LOGGER = logging.getLogger(__name__)


class BeurerCoordinator(DataUpdateCoordinator[dict]):
    """Push-driven coordinator for a single device.

    There is no polling interval: the hub pushes a status frame every few seconds
    and each one is published straight to the entities.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        hub: BeurerHub,
        client: BeurerClient,
        device: dict,
    ):
        super().__init__(hass, _LOGGER, name=f"{DOMAIN}_{device['id']}")
        self.hub = hub
        self.client = client
        self.device = device
        self.device_id: str = device["id"]
        self.model: str = device.get("model") or "unknown"
        self._last_status: float = 0.0
        self._first_status = asyncio.Event()
        # REST-only settings; these never appear in the status push.
        self.border_values: dict = {}
        self._unregister = hub.register(self.device_id, self._handle_status)
        hub.register_connection_listener(self._handle_connection_change)

    async def async_shutdown(self) -> None:
        self._unregister()
        await super().async_shutdown()

    async def async_refresh_border_values(self) -> None:
        """Re-read the comfort ranges and auto sensitivity."""
        try:
            self.border_values = await self.client.async_get_border_values(
                self.device_id
            )
        except BeurerError as err:
            # Not fatal: the rest of the device works without these.
            _LOGGER.warning("Could not read settings for %s: %s", self.device_id, err)

    async def async_wait_first_status(self) -> dict | None:
        """Wait briefly for the first status frame.

        Which entities make sense depends on what the model actually reports, and
        that is only visible in a status frame. If none arrives - device unplugged,
        cloud slow - setup continues anyway and the platforms fall back to the full
        default set rather than silently creating nothing.
        """
        try:
            await asyncio.wait_for(
                self._first_status.wait(), timeout=FIRST_STATUS_TIMEOUT
            )
        except TimeoutError:
            _LOGGER.info(
                "No status from %s within %ss; assuming the default entity set",
                self.device_id,
                FIRST_STATUS_TIMEOUT,
            )
            return None
        return self.data

    def supports(self, key: str) -> bool:
        """Whether the device reports a given status field.

        Before the first status frame this returns True, so nothing is dropped just
        because setup was faster than the first push.
        """
        if not self.data:
            return True
        return key in self.data

    async def async_send_command(self, function: str, value: int) -> None:
        """Send a command, surfacing failures the way Home Assistant expects.

        Entity actions must raise HomeAssistantError; anything else is reported to
        the user as an unhandled traceback rather than a readable message.
        """
        try:
            await self.hub.async_send_command(self.device_id, function, value)
        except BeurerError as err:
            raise HomeAssistantError(
                f"Could not reach the Beurer cloud to set {function}: {err}"
            ) from err

    async def async_set_sensitivity(self, value: str) -> None:
        """Change the auto-mode particle sensitivity, then re-read it."""
        if not self.border_values:
            await self.async_refresh_border_values()
        if not self.border_values:
            raise HomeAssistantError(
                "Beurer: current settings unknown, refusing to overwrite them"
            )

        try:
            await self.client.async_set_border_values(
                self.border_values, devicePmSensitivity=value
            )
        except BeurerError as err:
            raise HomeAssistantError(f"Could not change sensitivity: {err}") from err
        await self.async_refresh_border_values()
        self.async_update_listeners()

    @callback
    def _handle_status(self, status: dict) -> None:
        self._last_status = time.time()
        self._first_status.set()
        self.async_set_updated_data(status)

    @callback
    def _handle_connection_change(self) -> None:
        # Availability depends on the socket, so entities need to re-evaluate even
        # though no new data arrived.
        self.async_update_listeners()

    @property
    def available(self) -> bool:
        """Available only while the socket is up AND status is still arriving.

        A live socket alone is not enough: the cloud keeps the connection open even
        when the device itself has dropped off, so a stale timestamp is the only
        honest signal that the device is gone.
        """
        if not self.hub.connected or not self._last_status:
            return False
        return (time.time() - self._last_status) < STALE_AFTER

    async def _async_update_data(self) -> dict:
        # Never polled - data arrives via _handle_status.
        return self.data or {}
