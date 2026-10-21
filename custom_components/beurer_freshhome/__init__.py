"""The Beurer FreshHome integration."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD, Platform
from homeassistant.core import HomeAssistant, callback
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
    BeurerError,
    BeurerHub,
)
from .const import (
    CONF_REFRESH_TOKEN,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    client_secret_from_options,
)
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


async def async_migrate_entry(hass: HomeAssistant, entry: BeurerConfigEntry) -> bool:
    """Bring an entry created by an older release up to the current version.

    Version 1.1 entries hold the account password; 1.2 entries may hold a refresh
    token instead. This only records that the entry is now handled by code that
    understands both. It deliberately does NOT swap the password for a token:

      * that needs a sign-in, and a migration that fails on a network error is left
        in a migration-error state that Home Assistant does not retry, whereas
        setup retries by itself;
      * the password must not go until a refresh token has been stored AND Beurer
        has accepted it for a refresh, and only setup can find that out.

    So an entry that still has its password keeps working exactly as before, and
    async_setup_entry makes the swap once it is safe.
    """
    if entry.version > CONFIG_ENTRY_VERSION:
        # Written by a newer release than this one; nothing here can read it.
        return False

    if entry.minor_version < CONFIG_ENTRY_MINOR_VERSION:
        hass.config_entries.async_update_entry(
            entry, minor_version=CONFIG_ENTRY_MINOR_VERSION
        )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: BeurerConfigEntry) -> bool:
    """Set up Beurer FreshHome from a config entry."""
    email = entry.data[CONF_EMAIL]
    # Either may be missing: an entry holds the password until a refresh token has
    # been confirmed to work, and only the refresh token after that.
    password = entry.data.get(CONF_PASSWORD)
    refresh_token = entry.data.get(CONF_REFRESH_TOKEN)
    if not password and not refresh_token:
        raise ConfigEntryAuthFailed("No saved sign-in for this Beurer account")

    @callback
    def store_refresh_token(token: str) -> None:
        """Keep the token Beurer just issued, before anything else can need it.

        Beurer may rotate the refresh token on every use, in which case the one
        already stored stops working the moment the new one is issued. Losing the
        new one therefore costs the user a reauth, so it goes into the entry now.
        """
        if entry.data.get(CONF_REFRESH_TOKEN) != token:
            hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_REFRESH_TOKEN: token}
            )

    # One session for the whole entry: the hub's Azure App Service affinity cookie
    # is set at negotiate and has to still be present on the WebSocket upgrade.
    session = async_create_clientsession(hass)
    auth = BeurerAuth(
        session,
        email,
        password,
        client_secret_from_options(entry.options),
        refresh_token=refresh_token,
        on_refresh_token=store_refresh_token,
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

    await _async_replace_password_with_token(hass, entry, auth)

    # One hub per ACCOUNT. It receives every device's frames and dispatches them.
    hub = BeurerHub(session, auth)
    # A credential failure once running must reach the user, not just the log.
    hub.on_auth_error = lambda: entry.async_start_reauth(hass)
    # A client_secret rotation is not the user's password, so reauth is the wrong
    # prompt. Reloading puts it back through setup, which raises ConfigEntryError
    # with the message naming the override.
    hub.on_client_secret_error = lambda: hass.config_entries.async_schedule_reload(
        entry.entry_id
    )
    coordinators = [BeurerCoordinator(hass, hub, client, device) for device in devices]
    entry.runtime_data = BeurerRuntimeData(hub=hub, coordinators=coordinators)

    for coordinator in coordinators:
        await coordinator.async_refresh_border_values()

    await hub.async_start()

    # Give the devices a moment to report themselves, so the platforms can create
    # only the entities each model actually supports. Concurrently - waiting in turn
    # would multiply the timeout by the number of devices on the account.
    await asyncio.gather(*(c.async_wait_first_status() for c in coordinators))

    # Changing the client_secret override must rebuild the connection. Nothing else
    # about the entry should: the refresh token is rewritten into entry.data about
    # once an hour, and reloading each time would drop the connection for no reason.
    options = dict(entry.options)

    async def reload_when_options_change(
        hass: HomeAssistant, entry: BeurerConfigEntry
    ) -> None:
        if dict(entry.options) != options:
            await hass.config_entries.async_reload(entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(reload_when_options_change))

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        # Otherwise the hub and the coordinators' availability timers are left
        # running against an entry that never loaded.
        for coordinator in coordinators:
            await coordinator.async_shutdown()
        await hub.async_stop()
        raise

    return True


async def _async_replace_password_with_token(
    hass: HomeAssistant, entry: BeurerConfigEntry, auth: BeurerAuth
) -> None:
    """Drop the stored password, if a refresh token can stand in for it.

    "Can" is checked, not assumed. The token has to be in the entry already (the
    auth object puts it there as soon as Beurer issues it), and Beurer has to have
    accepted it for a real refresh - a token that was issued but is then refused
    would leave the user at the reauth prompt every time the access token expired.

    Anything short of that leaves the entry as it was, password included, and is
    tried again at the next setup. It is never an error: the entry works either way.
    """
    if CONF_PASSWORD not in entry.data:
        return

    try:
        confirmed = await auth.async_confirm_refresh_token()
    except BeurerError as err:
        _LOGGER.debug("Could not confirm the refresh token yet (%s)", err)
        return

    token = auth.refresh_token
    if not confirmed or not token:
        # Either no refresh token was issued, or Beurer refuses the one it issued.
        # Remove any stored one, so the entry goes back to being exactly what it
        # was rather than carrying a dead token that every setup would try first.
        if CONF_REFRESH_TOKEN in entry.data:
            hass.config_entries.async_update_entry(
                entry,
                data={k: v for k, v in entry.data.items() if k != CONF_REFRESH_TOKEN},
            )
        _LOGGER.info(
            "No usable refresh token from Beurer (none issued, or it was refused); "
            "the stored password is being kept"
        )
        return

    if entry.data.get(CONF_REFRESH_TOKEN) != token:
        # Not stored (the save callback failed). Keep the password.
        _LOGGER.warning(
            "The Beurer refresh token could not be saved; the stored password is "
            "being kept"
        )
        return

    hass.config_entries.async_update_entry(
        entry, data={k: v for k, v in entry.data.items() if k != CONF_PASSWORD}
    )
    _LOGGER.info("Replaced the stored Beurer password with a refresh token")


async def async_unload_entry(hass: HomeAssistant, entry: BeurerConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        for coordinator in entry.runtime_data.coordinators:
            await coordinator.async_shutdown()
        await entry.runtime_data.hub.async_stop()
    return unloaded
