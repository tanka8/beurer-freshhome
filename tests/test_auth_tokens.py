"""Refresh-token handling in BeurerAuth, with no network and no Home Assistant.

A config entry keeps the refresh token in place of the password, so what matters is
that every rotated token is handed over for saving, that a rejected token becomes a
"sign in again" and nothing worse, and that the token is trusted over the password
only once Beurer has actually accepted it.
"""

from __future__ import annotations

import asyncio
import json

import aiohttp
import pytest

from tests.loader import load_module

api = load_module("api")

EMAIL = "user@example.com"


class _Response:
    def __init__(self, status: int, body: str):
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class ScriptedSession:
    """Answers token requests from a queue and records what was asked."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.requests: list[dict] = []

    def post(self, url, data=None):
        self.requests.append(dict(data))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body = item
        return _Response(status, body if isinstance(body, str) else json.dumps(body))

    @property
    def grants(self) -> list[str]:
        return [request["grant_type"] for request in self.requests]


def ok(access: str = "access", refresh: str | None = None):
    body = {"access_token": access, "expires_in": 3600}
    if refresh is not None:
        body["refresh_token"] = refresh
    return 200, body


REJECTED = (400, {"error": "invalid_grant"})
BAD_SECRET = (400, {"error": "invalid_client"})
UNAVAILABLE = (503, "")


def make_auth(session, *, password=None, refresh_token=None):
    saved: list[str] = []
    auth = api.BeurerAuth(
        session,
        EMAIL,
        password,
        refresh_token=refresh_token,
        on_refresh_token=saved.append,
    )
    return auth, saved


def test_a_password_login_hands_over_the_refresh_token():
    session = ScriptedSession(ok("a1", "r1"))
    auth, saved = make_auth(session, password="pw")

    assert asyncio.run(auth.async_token()) == "a1"

    assert session.grants == ["password"]
    assert saved == ["r1"]
    assert auth.refresh_token == "r1"


def test_a_held_refresh_token_is_used_instead_of_the_password():
    session = ScriptedSession(ok("a1", "r2"))
    auth, saved = make_auth(session, password="pw", refresh_token="r1")

    asyncio.run(auth.async_token())

    assert session.grants == ["refresh_token"]
    assert session.requests[0]["refresh_token"] == "r1"
    assert "password" not in session.requests[0]
    assert saved == ["r2"]


def test_every_rotated_token_is_handed_over():
    """Beurer may retire the old token the moment it issues the next one."""
    session = ScriptedSession(ok("a1", "r2"), ok("a2", "r3"))
    auth, saved = make_auth(session, refresh_token="r1")

    async def scenario():
        await auth.async_token()
        auth._expires_at = 0.0  # the access token has run out
        await auth.async_token()

    asyncio.run(scenario())

    assert [r["refresh_token"] for r in session.requests] == ["r1", "r2"]
    assert saved == ["r2", "r3"]


def test_an_unchanged_refresh_token_is_not_handed_over_again():
    session = ScriptedSession(ok("a1", "r1"))
    auth, saved = make_auth(session, refresh_token="r1")

    asyncio.run(auth.async_token())

    assert saved == []


@pytest.mark.parametrize("extra", [{}, {"refresh_token": None}, {"refresh_token": ""}])
def test_a_response_without_a_refresh_token_keeps_the_one_held(extra):
    session = ScriptedSession((200, {"access_token": "a1", **extra}))
    auth, saved = make_auth(session, refresh_token="r1")

    asyncio.run(auth.async_token())

    assert auth.refresh_token == "r1"
    assert saved == []


def test_a_rejected_refresh_token_without_a_password_means_sign_in_again():
    """The only thing left to try is asking the user, and it must not loop."""
    session = ScriptedSession(REJECTED)
    auth, _ = make_auth(session, refresh_token="r1")

    async def scenario():
        for _ in range(2):
            with pytest.raises(api.BeurerAuthError):
                await auth.async_token()

    asyncio.run(scenario())

    # One request: no password to fall back on, and the dead token is not retried.
    assert session.grants == ["refresh_token"]


def test_a_rejected_refresh_token_falls_back_to_the_password():
    """Entries that still hold their password carry on as before."""
    session = ScriptedSession(REJECTED, ok("a1", "r9"))
    auth, saved = make_auth(session, password="pw", refresh_token="r1")

    assert asyncio.run(auth.async_token()) == "a1"

    assert session.grants == ["refresh_token", "password"]
    assert saved == ["r9"]


def test_a_rotated_client_secret_is_not_a_reauth_and_costs_no_token():
    """invalid_client is Beurer changing the app secret, not the user's problem."""
    session = ScriptedSession(BAD_SECRET)
    auth, _ = make_auth(session, password="pw", refresh_token="r1")

    with pytest.raises(api.BeurerClientSecretError) as raised:
        asyncio.run(auth.async_token())

    assert not isinstance(raised.value, api.BeurerAuthError)
    assert auth.refresh_token == "r1"
    assert session.grants == ["refresh_token"], "no password attempt either"


@pytest.mark.parametrize(
    "outage", [UNAVAILABLE, aiohttp.ClientConnectionError("unreachable")]
)
def test_an_outage_does_not_cost_the_user_their_token(outage):
    """Throwing the token away over a blip would turn it into a reauth."""
    session = ScriptedSession(outage)
    auth, _ = make_auth(session, refresh_token="r1")

    with pytest.raises(api.BeurerConnectionError):
        asyncio.run(auth.async_token())

    assert auth.refresh_token == "r1"


def test_no_credentials_at_all_is_an_auth_error_without_a_request():
    session = ScriptedSession()
    auth, _ = make_auth(session)

    with pytest.raises(api.BeurerAuthError):
        asyncio.run(auth.async_token())

    assert session.requests == []


def test_a_failing_save_does_not_fail_the_sign_in(caplog):
    """The sign-in worked; not being able to save is logged, not raised."""

    def broken(token: str) -> None:
        raise RuntimeError("storage unavailable")

    session = ScriptedSession(ok("a1", "r1"))
    auth = api.BeurerAuth(session, EMAIL, "pw", on_refresh_token=broken)

    assert asyncio.run(auth.async_token()) == "a1"

    assert auth.refresh_token == "r1"
    assert "Could not save the new Beurer refresh token" in caplog.text


def test_the_positional_signature_is_unchanged():
    """BeurerAuth(session, email, password, client_secret) is what older code uses."""
    session = ScriptedSession(ok("a1"))
    auth = api.BeurerAuth(session, EMAIL, "pw", "custom-secret")

    asyncio.run(auth.async_token())

    assert session.requests[0]["client_secret"] == "custom-secret"
    assert session.requests[0]["username"] == EMAIL
    assert session.requests[0]["password"] == "pw"


# --- async_confirm_refresh_token ------------------------------------------------


def test_confirming_spends_the_token_once():
    session = ScriptedSession(ok("a2", "r2"))
    auth, saved = make_auth(session, refresh_token="r1")

    async def scenario():
        return [await auth.async_confirm_refresh_token() for _ in range(2)]

    assert asyncio.run(scenario()) == [True, True]

    assert session.grants == ["refresh_token"], "the second answer needs no request"
    assert saved == ["r2"]


def test_a_refresh_that_already_worked_needs_no_second_one():
    session = ScriptedSession(ok("a1", "r2"))
    auth, _ = make_auth(session, password="pw", refresh_token="r1")

    async def scenario():
        await auth.async_token()
        return await auth.async_confirm_refresh_token()

    assert asyncio.run(scenario()) is True
    assert len(session.requests) == 1


def test_a_token_issued_by_a_password_login_is_not_yet_proof():
    """Issued is not the same as honoured: it has to be spent once."""
    session = ScriptedSession(ok("a1", "r1"), ok("a2", "r2"))
    auth, saved = make_auth(session, password="pw")

    async def scenario():
        await auth.async_token()
        return await auth.async_confirm_refresh_token()

    assert asyncio.run(scenario()) is True

    assert session.grants == ["password", "refresh_token"]
    assert saved == ["r1", "r2"]


def test_confirming_without_a_token_says_no_without_a_request():
    session = ScriptedSession()
    auth, _ = make_auth(session, password="pw")

    assert asyncio.run(auth.async_confirm_refresh_token()) is False
    assert session.requests == []


def test_a_refused_token_is_a_no_and_is_forgotten():
    session = ScriptedSession(REJECTED)
    auth, _ = make_auth(session, password="pw", refresh_token="r1")

    assert asyncio.run(auth.async_confirm_refresh_token()) is False
    assert auth.refresh_token is None


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        (UNAVAILABLE, api.BeurerConnectionError),
        (BAD_SECRET, api.BeurerClientSecretError),
    ],
)
def test_failing_to_find_out_is_not_a_no(answer, expected):
    """Failing to find out must reach the caller, which then keeps the password."""
    session = ScriptedSession(answer)
    auth, _ = make_auth(session, password="pw", refresh_token="r1")

    with pytest.raises(expected):
        asyncio.run(auth.async_confirm_refresh_token())

    assert auth.refresh_token == "r1"
