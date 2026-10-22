"""The refresh token that replaces the stored password.

Everything here goes through Home Assistant itself with only Beurer's HTTP endpoints
mocked, so it covers the wiring - the config entry, the reauth flow, the update
listener - as well as the logic in api.py.

The token endpoint is scripted: each test says exactly which answers Beurer gives,
in order, and a request nobody scripted fails the test.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMockResponse,
)

from custom_components.beurer_freshhome import BeurerRuntimeData
from custom_components.beurer_freshhome.api import BeurerClientSecretError
from custom_components.beurer_freshhome.const import (
    BORDER_VALUES_URL,
    CONF_CLIENT_SECRET,
    CONF_REFRESH_TOKEN,
    DEVICES_URL,
    DOMAIN,
    TOKEN_URL,
)
from custom_components.beurer_freshhome.diagnostics import (
    async_get_config_entry_diagnostics,
)

EMAIL = "user@example.com"
PASSWORD = "hunter2"
DEVICE = {"id": "0000000000000001", "name": "Air purifier", "model": "LR500"}
BORDER_VALUES = {
    "deviceId": DEVICE["id"],
    "deviceLocation": "manual",
    "devicePmSensitivity": "standard",
    "humidMin": 40,
    "humidMax": 60,
    "tempMin": 18,
    "tempMax": 25,
}

SETUP_ENTRY = "custom_components.beurer_freshhome.async_setup_entry"
LIST_DEVICES = "custom_components.beurer_freshhome.api.BeurerClient.async_list_devices"
START_HUB = "custom_components.beurer_freshhome.api.BeurerHub.async_start"
FIRST_STATUS = (
    "custom_components.beurer_freshhome.coordinator.BeurerCoordinator."
    "async_wait_first_status"
)


def tokens(access: str, refresh: str | None = None) -> tuple[int, dict]:
    body = {"access_token": access, "expires_in": 3600, "token_type": "Bearer"}
    if refresh:
        body["refresh_token"] = refresh
    return 200, body


REJECTED = (400, {"error": "invalid_grant"})
BAD_SECRET = (400, {"error": "invalid_client"})
UNAVAILABLE = (503, {})


@pytest.fixture
def token_endpoint(aioclient_mock):
    """Script the token endpoint: call with the answers, in the order they come."""

    def script(*answers):
        queue = list(answers)

        async def respond(method, url, data):
            status, body = queue.pop(0)
            return AiohttpClientMockResponse(method, url, status=status, json=body)

        aioclient_mock.post(TOKEN_URL, side_effect=respond)
        return queue  # what is left, so a test can check Beurer was asked it all

    return script


@pytest.fixture
def token_posts(aioclient_mock):
    """The forms sent to the token endpoint so far."""

    def posts() -> list[dict]:
        return [
            call[2]
            for call in aioclient_mock.mock_calls
            if str(call[1]).startswith(TOKEN_URL)
        ]

    return posts


@pytest.fixture
def cloud(aioclient_mock):
    """The REST side of Beurer's cloud."""
    aioclient_mock.get(DEVICES_URL, json={"devices": [DEVICE]})
    aioclient_mock.get(BORDER_VALUES_URL, json=BORDER_VALUES)


@pytest.fixture
def offline_hub():
    """No WebSocket in tests: do not start the hub, and do not wait for a frame."""
    with (
        patch(START_HUB, new_callable=AsyncMock),
        patch(FIRST_STATUS, new_callable=AsyncMock),
    ):
        yield


def make_entry(
    data: dict, *, version: int = 1, minor_version: int = 2
) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=EMAIL,
        title=EMAIL,
        data=data,
        version=version,
        minor_version=minor_version,
    )


async def set_up(hass, entry) -> None:
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def unload(hass, entry) -> None:
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


def grants(token_posts) -> list[str]:
    return [post["grant_type"] for post in token_posts()]


# --- setting up an entry ----------------------------------------------------------


@pytest.mark.usefixtures("cloud", "offline_hub")
async def test_setup_signs_in_with_the_refresh_token(hass, token_endpoint, token_posts):
    left = token_endpoint(tokens("a1", "r2"))
    entry = make_entry({CONF_EMAIL: EMAIL, CONF_REFRESH_TOKEN: "r1"})

    await set_up(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert grants(token_posts) == ["refresh_token"]
    assert token_posts()[0]["refresh_token"] == "r1"
    assert CONF_PASSWORD not in token_posts()[0]
    # The token Beurer sent back replaced the one that was used.
    assert entry.data == {CONF_EMAIL: EMAIL, CONF_REFRESH_TOKEN: "r2"}
    assert not left
    await unload(hass, entry)


@pytest.mark.usefixtures("cloud", "offline_hub")
async def test_an_old_entry_migrates_and_swaps_its_password_for_a_token(
    hass, token_endpoint, token_posts
):
    """Version 1.1 -> 1.2, the way an existing user's entry goes.

    It signs in with the password it has, saves the token that comes back, proves
    the token with a real refresh, and only then lets go of the password.
    """
    left = token_endpoint(tokens("a1", "r1"), tokens("a2", "r2"))
    entry = make_entry({CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}, minor_version=1)

    await set_up(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.minor_version == 2
    assert grants(token_posts) == ["password", "refresh_token"]
    assert token_posts()[1]["refresh_token"] == "r1"
    assert entry.data == {CONF_EMAIL: EMAIL, CONF_REFRESH_TOKEN: "r2"}
    assert PASSWORD not in json.dumps(dict(entry.data))
    assert not left
    await unload(hass, entry)


@pytest.mark.usefixtures("cloud", "offline_hub")
async def test_an_old_entry_keeps_its_password_if_beurer_refuses_the_token(
    hass, token_endpoint, token_posts
):
    """A token that was issued but is not honoured must never replace the password."""
    left = token_endpoint(tokens("a1", "r1"), REJECTED)
    entry = make_entry({CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}, minor_version=1)

    await set_up(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.data == {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    assert not left
    await unload(hass, entry)


@pytest.mark.usefixtures("cloud", "offline_hub")
async def test_an_old_entry_keeps_its_password_if_no_token_is_issued(
    hass, token_endpoint, token_posts
):
    """Nothing in the response to swap for: the entry carries on exactly as before."""
    left = token_endpoint(tokens("a1"))
    entry = make_entry({CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}, minor_version=1)

    await set_up(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert grants(token_posts) == ["password"]
    assert entry.data == {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    assert not left
    await unload(hass, entry)


@pytest.mark.usefixtures("cloud", "offline_hub")
async def test_an_old_entry_keeps_its_password_until_the_token_can_be_checked(
    hass, token_endpoint
):
    """An outage while confirming is "could not tell", not "no".

    The token is kept, so the next setup can finish the job; the password stays.
    """
    left = token_endpoint(tokens("a1", "r1"), UNAVAILABLE)
    entry = make_entry({CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}, minor_version=1)

    await set_up(hass, entry)

    assert entry.state is ConfigEntryState.LOADED
    assert entry.data == {
        CONF_EMAIL: EMAIL,
        CONF_PASSWORD: PASSWORD,
        CONF_REFRESH_TOKEN: "r1",
    }
    assert not left
    await unload(hass, entry)


@pytest.mark.usefixtures("cloud", "offline_hub")
async def test_an_entry_holding_both_finishes_the_swap_without_a_second_refresh(
    hass, token_endpoint, token_posts
):
    """The refresh that signs in already proved the token, so nothing more is asked."""
    left = token_endpoint(tokens("a1", "r2"))
    entry = make_entry(
        {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD, CONF_REFRESH_TOKEN: "r1"}
    )

    await set_up(hass, entry)

    assert grants(token_posts) == ["refresh_token"]
    assert entry.data == {CONF_EMAIL: EMAIL, CONF_REFRESH_TOKEN: "r2"}
    assert not left
    await unload(hass, entry)


async def test_an_entry_from_a_newer_release_is_not_touched(hass, aioclient_mock):
    entry = make_entry({CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}, version=2)

    await set_up(hass, entry)

    assert entry.state is ConfigEntryState.MIGRATION_ERROR
    assert entry.data == {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
    assert aioclient_mock.call_count == 0


# --- while running ----------------------------------------------------------------


@pytest.mark.usefixtures("cloud", "offline_hub")
async def test_a_rotated_token_is_saved_and_does_not_reload_the_entry(
    hass, aioclient_mock, token_endpoint
):
    left = token_endpoint(tokens("a1", "r2"), tokens("a2", "r3"))
    entry = make_entry({CONF_EMAIL: EMAIL, CONF_REFRESH_TOKEN: "r1"})
    await set_up(hass, entry)
    runtime = entry.runtime_data

    # The access token runs out; the next call refreshes and Beurer rotates again.
    auth = runtime.coordinators[0].client._auth
    auth._expires_at = 0.0
    await auth.async_token()
    await hass.async_block_till_done()

    assert entry.data[CONF_REFRESH_TOKEN] == "r3"
    # Saving a token changes entry.data, which must not look like an options change:
    # a reload every hour would drop the connection each time.
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is runtime
    listed = [c for c in aioclient_mock.mock_calls if str(c[1]).startswith(DEVICES_URL)]
    assert len(listed) == 1
    assert not left
    await unload(hass, entry)


@pytest.mark.usefixtures("cloud", "offline_hub")
async def test_changing_the_client_secret_still_reloads_the_entry(
    hass, token_endpoint, token_posts
):
    token_endpoint(tokens("a1", "r2"), tokens("a2", "r3"))
    entry = make_entry({CONF_EMAIL: EMAIL, CONF_REFRESH_TOKEN: "r1"})
    await set_up(hass, entry)
    runtime = entry.runtime_data

    hass.config_entries.async_update_entry(
        entry, options={CONF_CLIENT_SECRET: "new-secret"}
    )
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not runtime
    assert token_posts()[-1]["client_secret"] == "new-secret"
    assert entry.data[CONF_REFRESH_TOKEN] == "r3"
    await unload(hass, entry)


# --- when Beurer says no ----------------------------------------------------------


@pytest.mark.usefixtures("cloud", "offline_hub")
async def test_a_rejected_refresh_token_asks_for_the_password(hass, token_endpoint):
    left = token_endpoint(REJECTED)
    entry = make_entry({CONF_EMAIL: EMAIL, CONF_REFRESH_TOKEN: "r1"})

    await set_up(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == [
        config_entries.SOURCE_REAUTH
    ]
    assert flows[0]["context"]["entry_id"] == entry.entry_id
    assert not left


@pytest.mark.usefixtures("cloud", "offline_hub")
async def test_a_rotated_client_secret_is_still_not_a_reauth(hass, token_endpoint):
    """invalid_client is handled exactly as before: an error, not a password prompt."""
    token_endpoint(BAD_SECRET)
    entry = make_entry({CONF_EMAIL: EMAIL, CONF_REFRESH_TOKEN: "r1"})

    await set_up(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert "client_secret" in entry.reason
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []
    # And the token survives: nothing was wrong with it.
    assert entry.data[CONF_REFRESH_TOKEN] == "r1"


@pytest.mark.usefixtures("cloud", "offline_hub")
async def test_an_outage_retries_and_keeps_the_token(hass, token_endpoint):
    token_endpoint(UNAVAILABLE)
    entry = make_entry({CONF_EMAIL: EMAIL, CONF_REFRESH_TOKEN: "r1"})

    await set_up(hass, entry)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    assert entry.data == {CONF_EMAIL: EMAIL, CONF_REFRESH_TOKEN: "r1"}
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []
    await unload(hass, entry)


# --- reauth -----------------------------------------------------------------------


async def _start_reauth(hass, entry) -> str:
    entry.add_to_hass(hass)
    entry.async_start_reauth(hass)
    await hass.async_block_till_done()
    (flow,) = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    return flow["flow_id"]


@pytest.mark.usefixtures("cloud")
@pytest.mark.parametrize(
    "stale",
    [
        pytest.param({CONF_REFRESH_TOKEN: "expired"}, id="expired-token"),
        pytest.param({CONF_PASSWORD: "old-password"}, id="old-password"),
    ],
)
async def test_reauth_stores_a_refresh_token_and_not_the_password(
    hass, token_endpoint, token_posts, stale
):
    left = token_endpoint(tokens("a1", "r10"), tokens("a2", "r11"))
    entry = make_entry({CONF_EMAIL: EMAIL, **stale})
    flow_id = await _start_reauth(hass, entry)

    with patch(SETUP_ENTRY, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_PASSWORD: PASSWORD}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert grants(token_posts) == ["password", "refresh_token"]
    assert entry.data == {CONF_EMAIL: EMAIL, CONF_REFRESH_TOKEN: "r11"}
    assert PASSWORD not in json.dumps(dict(entry.data))
    assert not left


@pytest.mark.usefixtures("cloud")
async def test_reauth_keeps_the_password_if_the_token_is_not_honoured(
    hass, token_endpoint
):
    token_endpoint(tokens("a1", "r10"), REJECTED)
    entry = make_entry({CONF_EMAIL: EMAIL, CONF_REFRESH_TOKEN: "expired"})
    flow_id = await _start_reauth(hass, entry)

    with patch(SETUP_ENTRY, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            flow_id, {CONF_PASSWORD: PASSWORD}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert entry.data == {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}


async def test_reauth_with_the_wrong_password_changes_nothing(hass, token_endpoint):
    token_endpoint(REJECTED)
    entry = make_entry({CONF_EMAIL: EMAIL, CONF_REFRESH_TOKEN: "expired"})
    flow_id = await _start_reauth(hass, entry)

    result = await hass.config_entries.flow.async_configure(
        flow_id, {CONF_PASSWORD: "wrong"}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}
    assert entry.data == {CONF_EMAIL: EMAIL, CONF_REFRESH_TOKEN: "expired"}


# --- adding the integration -------------------------------------------------------


async def _add(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(SETUP_ENTRY, return_value=True):
        return await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}
        )


@pytest.mark.usefixtures("cloud")
async def test_a_new_entry_stores_the_refresh_token_not_the_password(
    hass, token_endpoint, token_posts
):
    left = token_endpoint(tokens("a1", "r1"), tokens("a2", "r2"))

    result = await _add(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_EMAIL: EMAIL, CONF_REFRESH_TOKEN: "r2"}
    assert (result["result"].version, result["result"].minor_version) == (1, 2)
    assert grants(token_posts) == ["password", "refresh_token"]
    assert token_posts()[1]["refresh_token"] == "r1"
    assert not left


@pytest.mark.usefixtures("cloud")
@pytest.mark.parametrize(
    "answers",
    [
        pytest.param([tokens("a1")], id="no-token-issued"),
        pytest.param([tokens("a1", "r1"), REJECTED], id="token-refused"),
        pytest.param([tokens("a1", "r1"), UNAVAILABLE], id="cannot-check"),
    ],
)
async def test_a_new_entry_keeps_the_password_when_the_token_cannot_be_trusted(
    hass, token_endpoint, answers
):
    token_endpoint(*answers)

    result = await _add(hass)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}


# --- the options flow -------------------------------------------------------------


async def test_options_without_a_stored_password_do_not_sign_in(hass, aioclient_mock):
    """There is no password to check a secret with, and the token is not to be spent.

    The secret is saved and the entry reloads with it; setup is what reports a
    rejected one.
    """
    entry = make_entry({CONF_EMAIL: EMAIL, CONF_REFRESH_TOKEN: "r1"})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_CLIENT_SECRET: "new-secret"}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_CLIENT_SECRET: "new-secret"}
    assert entry.data == {CONF_EMAIL: EMAIL, CONF_REFRESH_TOKEN: "r1"}
    assert aioclient_mock.call_count == 0


async def test_options_with_a_stored_password_still_check_the_secret(hass):
    """Unchanged for entries that have not been migrated yet."""
    entry = make_entry({CONF_EMAIL: EMAIL, CONF_PASSWORD: PASSWORD}, minor_version=1)
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    with patch(LIST_DEVICES, side_effect=BeurerClientSecretError("rotated")):
        result = await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_CLIENT_SECRET: "wrong"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_client_secret"}
    assert entry.options == {}


# --- diagnostics ------------------------------------------------------------------


async def test_diagnostics_never_contain_a_credential(hass):
    secrets = {
        CONF_EMAIL: EMAIL,
        CONF_PASSWORD: "a-password-that-must-not-leak",
        CONF_REFRESH_TOKEN: "a-refresh-token-that-must-not-leak",
    }
    entry = make_entry(secrets)
    entry.add_to_hass(hass)
    entry.runtime_data = BeurerRuntimeData(hub=MagicMock(connected=True))

    diagnostics = await async_get_config_entry_diagnostics(hass, entry)

    dumped = json.dumps(diagnostics)
    for value in secrets.values():
        assert value not in dumped
    # The keys stay, so a report shows what kind of sign-in the entry holds.
    assert set(diagnostics["config"]) == set(secrets)
