"""Classification of rejected token requests.

Telling a bad client_secret apart from a bad password matters: the first affects
every user at once and is fixed by the override, while the second should send that
one user to reauth. Getting it backwards means telling people to retype a password
that was never wrong.
"""

import json

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
