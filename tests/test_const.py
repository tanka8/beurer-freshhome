"""Model handling - the part most likely to be wrong on an untested device."""

from tests.loader import load_module

const = load_module("const")


def test_known_model():
    assert const.fan_speeds("LR500") == [1, 2, 3, 4, 5]
    assert const.fan_speeds("lr500") == [1, 2, 3, 4, 5], "must be case insensitive"


def test_unknown_model_falls_back():
    """An unfamiliar model must still produce a usable entity, not an exception."""
    assert const.fan_speeds("LR9999") == const.DEFAULT_FAN_SPEEDS
    assert const.fan_speeds("") == const.DEFAULT_FAN_SPEEDS
    assert const.fan_speeds(None) == const.DEFAULT_FAN_SPEEDS


def test_top_speed_is_labelled_turbo():
    names = const.fan_speed_names("LR500")
    assert names[5] == "turbo"
    assert names[1] == "1"
    assert len(names) == 5


def test_speed_names_round_trip():
    names = const.fan_speed_names("LR500")
    assert {v: k for k, v in names.items()}["turbo"] == 5


def test_client_secret_override():
    """A rotation by Beurer must be fixable without a release."""
    assert const.client_secret_from_options({}) == const.DEFAULT_CLIENT_SECRET
    assert const.client_secret_from_options({"client_secret": "new"}) == "new"
    # Blank means "fall back to the bundled value", not "send an empty secret".
    assert const.client_secret_from_options({"client_secret": ""}) == const.DEFAULT_CLIENT_SECRET
