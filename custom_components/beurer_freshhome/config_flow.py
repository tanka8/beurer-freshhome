"""Config flow for Beurer FreshHome."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from .api import BeurerAuth, BeurerAuthError, BeurerClient, BeurerConnectionError
from .const import DOMAIN

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): str})


class BeurerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for the Beurer account credentials and verify them."""

    VERSION = 1

    async def _async_validate(self, email: str, password: str) -> str | None:
        """Return an error key, or None if the credentials work."""
        session = async_create_clientsession(self.hass)
        client = BeurerClient(session, BeurerAuth(session, email, password))
        try:
            devices = await client.async_list_devices(email)
        except BeurerAuthError:
            return "invalid_auth"
        except BeurerConnectionError:
            return "cannot_connect"
        if not devices:
            return "no_devices"
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL]
            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_configured()

            error = await self._async_validate(email, user_input[CONF_PASSWORD])
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
        """Triggered when the stored password stops working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()

        if user_input is not None:
            email = entry.data[CONF_EMAIL]
            error = await self._async_validate(email, user_input[CONF_PASSWORD])
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
