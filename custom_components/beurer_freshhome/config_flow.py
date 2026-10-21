"""Config flow for Beurer FreshHome."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    BeurerAuth,
    BeurerAuthError,
    BeurerClient,
    BeurerClientSecretError,
    BeurerConnectionError,
    BeurerError,
)
from .const import (
    CONF_CLIENT_SECRET,
    CONF_REFRESH_TOKEN,
    CONFIG_ENTRY_MINOR_VERSION,
    CONFIG_ENTRY_VERSION,
    DEFAULT_CLIENT_SECRET,
    DOMAIN,
    client_secret_from_options,
)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})


async def _sign_in(
    hass, email: str, password: str, client_secret: str
) -> tuple[str | None, BeurerAuth]:
    """Try the credentials. Returns an error key (None if they work) and the auth."""
    # Home Assistant's shared session, not a new one per attempt: sessions created
    # here are only closed when Home Assistant stops, so a handful of mistyped
    # passwords would leave a handful of sessions behind. The dedicated session in
    # __init__ exists for the hub's affinity cookie, which none of this needs.
    session = async_get_clientsession(hass)
    auth = BeurerAuth(session, email, password, client_secret)
    client = BeurerClient(session, auth)
    try:
        devices = await client.async_list_devices(email)
    except BeurerClientSecretError:
        return "invalid_client_secret", auth
    except BeurerAuthError:
        return "invalid_auth", auth
    except BeurerConnectionError:
        return "cannot_connect", auth
    if not devices:
        return "no_devices", auth
    return None, auth


async def _validate(hass, email: str, password: str, client_secret: str) -> str | None:
    """Return an error key, or None if the credentials work."""
    error, _ = await _sign_in(hass, email, password, client_secret)
    return error


async def _validate_for_storage(
    hass, email: str, password: str, client_secret: str
) -> tuple[str | None, str | None]:
    """Like _validate, but also say what is safe to store in place of the password.

    The second item is a refresh token, and only when Beurer has accepted it for a
    real refresh. A token that was issued but never proven is not returned: storing
    it instead of the password would leave the user at the reauth prompt every time
    the access token expired, if Beurer then refused it.
    """
    error, auth = await _sign_in(hass, email, password, client_secret)
    if error:
        return error, None
    try:
        confirmed = await auth.async_confirm_refresh_token()
    except BeurerError:
        # Could not tell (the network, say), which is not the same as "no". The
        # sign-in itself worked, so keep the password; setup retries the swap.
        confirmed = False
    return None, auth.refresh_token if confirmed else None


def _stored_credentials(password: str, refresh_token: str | None) -> dict[str, str]:
    """What an entry keeps to sign in with.

    The refresh token when Beurer has confirmed it works, so the password is not
    kept at all; otherwise the password, exactly as before.
    """
    if refresh_token:
        return {CONF_REFRESH_TOKEN: refresh_token}
    return {CONF_PASSWORD: password}


class BeurerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for the Beurer account credentials and verify them."""

    VERSION = CONFIG_ENTRY_VERSION
    MINOR_VERSION = CONFIG_ENTRY_MINOR_VERSION

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return BeurerOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL]
            password = user_input[CONF_PASSWORD]
            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_configured()

            error, refresh_token = await _validate_for_storage(
                self.hass, email, password, DEFAULT_CLIENT_SECRET
            )
            if error:
                errors["base"] = error
            else:
                credentials = _stored_credentials(password, refresh_token)
                return self.async_create_entry(
                    title=email, data={CONF_EMAIL: email, **credentials}
                )

        return self.async_show_form(
            step_id="user", data_schema=USER_SCHEMA, errors=errors
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Triggered when the stored credentials stop working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            error, refresh_token = await _validate_for_storage(
                self.hass,
                entry.data[CONF_EMAIL],
                password,
                client_secret_from_options(entry.options),
            )
            if error:
                errors["base"] = error
            else:
                # Replace the stored sign-in rather than add to it: whichever of
                # password and token the entry held is stale, and the new one must
                # not sit alongside it.
                data = {
                    key: value
                    for key, value in entry.data.items()
                    if key not in (CONF_PASSWORD, CONF_REFRESH_TOKEN)
                }
                data.update(_stored_credentials(password, refresh_token))
                return self.async_update_reload_and_abort(entry, data=data)

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            description_placeholders={"email": entry.data[CONF_EMAIL]},
            errors=errors,
        )


class BeurerOptionsFlow(OptionsFlow):
    """Lets the app client_secret be replaced without a new release."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self.config_entry

        if user_input is not None:
            # Blank means "go back to the bundled value".
            secret = (user_input.get(CONF_CLIENT_SECRET) or "").strip()

            # The new secret is tried by signing in with the stored password. An
            # entry that no longer has one cannot be checked that way, and the
            # refresh token must not be used for it: signing in with it can make
            # Beurer retire it in favour of a new one, which a check made here
            # could not hand to the connection that is running. Such an entry is
            # simply reloaded with the new secret, and setup reports a rejected
            # one with the message that names this option.
            password = entry.data.get(CONF_PASSWORD)
            error = None
            if password:
                error = await _validate(
                    self.hass,
                    entry.data[CONF_EMAIL],
                    password,
                    secret or DEFAULT_CLIENT_SECRET,
                )
            if error:
                errors["base"] = error
            else:
                return self.async_create_entry(
                    data={CONF_CLIENT_SECRET: secret} if secret else {}
                )

        current = entry.options.get(CONF_CLIENT_SECRET, "")
        schema = vol.Schema({vol.Optional(CONF_CLIENT_SECRET, default=current): str})
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
