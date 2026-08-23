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
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import (
    BeurerAuth,
    BeurerAuthError,
    BeurerClient,
    BeurerClientSecretError,
    BeurerConnectionError,
)
from .const import (
    CONF_CLIENT_SECRET,
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


async def _validate(hass, email: str, password: str, client_secret: str) -> str | None:
    """Return an error key, or None if the credentials work."""
    session = async_create_clientsession(hass)
    client = BeurerClient(session, BeurerAuth(session, email, password, client_secret))
    try:
        devices = await client.async_list_devices(email)
    except BeurerClientSecretError:
        return "invalid_client_secret"
    except BeurerAuthError:
        return "invalid_auth"
    except BeurerConnectionError:
        return "cannot_connect"
    if not devices:
        return "no_devices"
    return None


class BeurerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for the Beurer account credentials and verify them."""

    VERSION = 1

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
            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_configured()

            error = await _validate(
                self.hass, email, user_input[CONF_PASSWORD], DEFAULT_CLIENT_SECRET
            )
            if error:
                errors["base"] = error
            else:
                return self.async_create_entry(title=email, data=user_input)

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
            error = await _validate(
                self.hass,
                entry.data[CONF_EMAIL],
                user_input[CONF_PASSWORD],
                client_secret_from_options(entry.options),
            )
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )

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
            error = await _validate(
                self.hass,
                entry.data[CONF_EMAIL],
                entry.data[CONF_PASSWORD],
                secret or DEFAULT_CLIENT_SECRET,
            )
            if error:
                errors["base"] = error
            else:
                return self.async_create_entry(
                    data={CONF_CLIENT_SECRET: secret} if secret else {}
                )

        current = entry.options.get(CONF_CLIENT_SECRET, "")
        schema = vol.Schema(
            {vol.Optional(CONF_CLIENT_SECRET, default=current): str}
        )
        return self.async_show_form(
            step_id="init", data_schema=schema, errors=errors
        )
