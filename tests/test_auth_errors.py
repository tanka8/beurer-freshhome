"""Classification of rejected token requests.

Telling a bad client_secret apart from a bad password matters: the first affects
every user at once and is fixed by the override, while the second should send that
one user to reauth. Getting it backwards means telling people to retype a password
that was never wrong.
"""

import asyncio
import json

import pytest

from tests.loader import load_module

api = load_module("api")


def test_invalid_client_is_a_secret_problem():
    err = api._token_error(400, json.dumps({"error": "invalid_client"}))
    assert isinstance(err, api.BeurerClientSecretError)
    assert not isinstance(err, api.BeurerAuthError)


def test_invalid_grant_is_a_password_problem():
    err = api._token_error(400, json.dumps({"error": "invalid_grant"}))
    assert isinstance(err, api.BeurerAuthError)
    assert not isinstance(err, api.BeurerClientSecretError)


def test_unknown_error_falls_back_to_auth():
    err = api._token_error(400, json.dumps({"error": "something_new"}))
    assert isinstance(err, api.BeurerAuthError)


def test_non_json_body_does_not_raise():
    """A server that ignores the spec must not crash the classifier."""
    err = api._token_error(401, "<html>gateway error</html>")
    assert isinstance(err, api.BeurerAuthError)


def test_empty_body():
    assert isinstance(api._token_error(400, ""), api.BeurerAuthError)


def test_both_are_beurer_errors():
    """Callers that only care about failure can still catch the base class."""
    for body in ('{"error": "invalid_client"}', '{"error": "invalid_grant"}'):
        assert isinstance(api._token_error(400, body), api.BeurerError)


class _FakeResponse:
    """Just enough of an aiohttp response for the token request."""

    def __init__(self, status: int, body: str):
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self._response = response

    def post(self, url, data=None):
        return self._response


def _token(status: int, body: str):
    auth = api.BeurerAuth(_FakeSession(_FakeResponse(status, body)), "e", "p")
    return asyncio.run(auth.async_token())


def test_a_good_token_response_is_stored():
    assert _token(200, json.dumps({"access_token": "abc", "expires_in": 60})) == "abc"


@pytest.mark.parametrize(
    "body",
    [
        "<html>not json at all</html>",
        json.dumps({"token_type": "Bearer"}),  # 200, but no access_token
        json.dumps(["unexpected", "shape"]),
    ],
)
def test_a_malformed_token_response_stays_a_beurer_error(body):
    """Anything that escapes BeurerError reaches the user as a raw traceback.

    Every caller in the integration catches BeurerError and nothing wider, so a
    JSONDecodeError or KeyError here would bypass the config flow's error handling
    and the coordinator's HomeAssistantError translation alike.
    """
    with pytest.raises(api.BeurerError):
        _token(200, body)


def test_a_nonsense_expiry_does_not_break_the_login():
    """A bad expires_in must not cost the caller a token it successfully fetched."""
    assert (
        _token(200, json.dumps({"access_token": "abc", "expires_in": "soon"})) == "abc"
    )
