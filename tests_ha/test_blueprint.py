"""The turbo-on-high-PM2.5 blueprint, loaded and run by Home Assistant itself.

Home Assistant validates a blueprint when it is instantiated, not when it is
written, so a typo would otherwise be found by whoever imports it. These tests load
it through the real blueprint machinery and then drive an automation made from it.
"""

import shutil
from datetime import timedelta
from pathlib import Path

import pytest
from homeassistant.components.automation.config import AUTOMATION_BLUEPRINT_SCHEMA
from homeassistant.components.blueprint import models
from homeassistant.helpers.config_validation import ensure_list
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util.yaml import load_yaml_dict
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    async_mock_service,
)

BLUEPRINT = (
    Path(__file__).parent.parent
    / "blueprints"
    / "automation"
    / "beurer_freshhome"
    / "turbo_on_high_pm25.yaml"
)

PM25 = "sensor.purifier_pm2_5"
FAN = "fan.purifier"


def test_the_blueprint_is_valid():
    blueprint = models.Blueprint(
        load_yaml_dict(BLUEPRINT),
        expected_domain="automation",
        schema=AUTOMATION_BLUEPRINT_SCHEMA,
    )
    assert set(blueprint.inputs) == {
        "pm25_sensor",
        "purifier",
        "high_threshold",
        "high_for",
        "restore_auto",
        "low_threshold",
        "low_for",
    }


@pytest.fixture
async def automation(hass, tmp_path):
    """An automation made from the blueprint with its default thresholds.

    Turbo above 20 for 5 minutes; back to Auto below 10 for 15 minutes.
    """
    folder = tmp_path / "blueprints" / "automation" / "beurer_freshhome"
    folder.mkdir(parents=True)
    shutil.copy(BLUEPRINT, folder)
    hass.config.config_dir = str(tmp_path)

    hass.states.async_set(PM25, "5")
    set_fan(hass, percentage=40, preset_mode="auto")

    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "use_blueprint": {
                    "path": "beurer_freshhome/turbo_on_high_pm25.yaml",
                    "input": {"pm25_sensor": PM25, "purifier": FAN},
                }
            }
        },
    )
    await hass.async_block_till_done()


def set_fan(hass, *, percentage, preset_mode):
    hass.states.async_set(
        FAN, "on", {"percentage": percentage, "preset_mode": preset_mode}
    )


async def wait(hass, minutes):
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=minutes))
    await hass.async_block_till_done()


@pytest.mark.usefixtures("automation")
async def test_turbo_after_pm25_stays_high(hass):
    turn_on = async_mock_service(hass, "fan", "turn_on")

    hass.states.async_set(PM25, "40")
    await hass.async_block_till_done()
    await wait(hass, 2)
    assert turn_on == [], "a short spike is ignored"

    await wait(hass, 6)
    assert len(turn_on) == 1
    assert ensure_list(turn_on[0].data["entity_id"]) == [FAN]
    assert turn_on[0].data["percentage"] == 100


@pytest.mark.usefixtures("automation")
async def test_back_to_auto_once_the_air_is_clean(hass):
    set_preset = async_mock_service(hass, "fan", "set_preset_mode")
    set_fan(hass, percentage=100, preset_mode="manual")  # at Turbo, as left by turbo

    hass.states.async_set(PM25, "40")
    await hass.async_block_till_done()
    hass.states.async_set(PM25, "5")
    await hass.async_block_till_done()
    await wait(hass, 20)

    assert len(set_preset) == 1
    assert ensure_list(set_preset[0].data["entity_id"]) == [FAN]
    assert set_preset[0].data["preset_mode"] == "auto"


@pytest.mark.usefixtures("automation")
async def test_a_speed_chosen_since_is_left_alone(hass):
    set_preset = async_mock_service(hass, "fan", "set_preset_mode")
    set_fan(hass, percentage=60, preset_mode="manual")  # the user picked this

    hass.states.async_set(PM25, "40")
    await hass.async_block_till_done()
    hass.states.async_set(PM25, "5")
    await hass.async_block_till_done()
    await wait(hass, 20)

    assert set_preset == []
