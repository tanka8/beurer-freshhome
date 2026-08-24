"""The Beurer FreshHome integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryError,
    ConfigEntryNotReady,
)
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import (
    BeurerAuth,
    BeurerAuthError,
    BeurerClient,
    BeurerClientSecretError,
    BeurerConnectionError,
    BeurerHub,
)
from .const import client_secret_from_options
from .coordinator import BeurerCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.FAN,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]


@dataclass
class BeurerRuntimeData:
    """Everything a loaded config entry owns."""

    hub: BeurerHub
    coordinators: list[BeurerCoordinator] = field(default_factory=list)


type BeurerConfigEntry = ConfigEntry[BeurerRuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: BeurerConfigEntry) -> bool:
    """Set up Beurer FreshHome from a config entry."""
    email = entry.data[CONF_EMAIL]
    password = entry.data[CONF_PASSWORD]

    # One session for the whole entry: the hub's Azure App Service affinity cookie
    # is set at negotiate and has to still be present on the WebSocket upgrade.
    session = async_create_clientsession(hass)
    auth = BeurerAuth(
        session, email, password, client_secret_from_options(entry.options)
    )
    client = BeurerClient(session, auth)

    try:
        devices = await client.async_list_devices(email)
    except BeurerClientSecretError as err:
        # Not the user's password. Sending them to reauth would have them retype a
        # password that was never wrong; the fix is the override in the options.
        raise ConfigEntryError(str(err)) from err
    except BeurerAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except BeurerConnectionError as err:
        raise ConfigEntryNotReady(str(err)) from err

    if not devices:
        raise ConfigEntryNotReady(f"No devices on the Beurer account {email}")

    # One hub per ACCOUNT. It receives every device's frames and dispatches them.
    hub = BeurerHub(session, auth)
    # A credential failure once running must reach the user, not just the log.
    hub.on_auth_error = lambda: entry.async_start_reauth(hass)
    coordinators = [BeurerCoordinator(hass, hub, client, device) for device in devices]
    entry.runtime_data = BeurerRuntimeData(hub=hub, coordinators=coordinators)

    for coordinator in coordinators:
        await coordinator.async_refresh_border_values()

    await hub.async_start()

    # Give the devices a moment to report themselves, so the platforms can create
    # only the entities each model actually supports. Concurrently - waiting in turn
    # would multiply the timeout by the number of devices on the account.
    await asyncio.gather(*(c.async_wait_first_status() for c in coordinators))

    # Changing the client_secret override must rebuild the connection.
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        # Otherwise the hub is left running against an entry that never loaded.
        await hub.async_stop()
        raise

    return True


async def async_unload_entry(hass: HomeAssistant, entry: BeurerConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        for coordinator in entry.runtime_data.coordinators:
            await coordinator.async_shutdown()
        await entry.runtime_data.hub.async_stop()
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: BeurerConfigEntry) -> None:
    """Reload when the options change."""
    await hass.config_entries.async_reload(entry.entry_id)
