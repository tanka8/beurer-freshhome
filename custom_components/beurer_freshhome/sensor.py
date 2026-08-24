"""Sensor entities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    UnitOfTemperature,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BeurerConfigEntry
from .const import AIR_QUALITY_LABELS
from .entity import BeurerEntity

try:  # Home Assistant 2026.7 and newer
    from homeassistant.const import UnitOfDensity

    MICROGRAMS_PER_CUBIC_METER = UnitOfDensity.MICROGRAMS_PER_CUBIC_METER
except ImportError:  # Home Assistant older than 2026.7
    # UnitOfDensity did not exist before 2026.7. Importing it unconditionally would
    # have made the integration unloadable on every core older than that, which is
    # most of the range this integration claims to support. The older spelling is
    # only touched on the versions where it is not deprecated, so no deprecation
    # warning is produced on a current core.
    from homeassistant.const import (
        CONCENTRATION_MICROGRAMS_PER_CUBIC_METER as MICROGRAMS_PER_CUBIC_METER,
    )

# Commands all travel over the one shared WebSocket and are cheap, so there is no
# reason to serialise them. Nothing here polls.
PARALLEL_UPDATES = 0


def _temperature(status: dict) -> float | None:
    """Decode the reported temperature.

    Observed values were 5376 and 5632, i.e. exactly 21*256 and 22*256, so this is a
    16-bit fixed-point value with the whole degrees in the high byte. Every sample so
    far had a zero low byte, so whether that byte carries fractions is still unproven
    - if it does, this already handles it; if it does not, the result is unchanged.
    Either way the whole-degree part is correct.
    """
    raw = status.get("temperature")
    if raw is None:
        return None
    return round(raw / 256, 1)


def _pm25(status: dict) -> float | None:
    """Decode PM2.5, which the device reports in tenths of a ug/m3.

    Established by correlating 538 captured status frames against the device's own
    airquality band: the good/moderate boundary sits exactly at pm 100/101, and the
    bands land on 10, ~20 and ~25 ug/m3 once divided by ten - the standard WHO-derived
    thresholds. Read as raw ug/m3 the device would be calling 100 ug/m3 "good", which
    no air quality scale does.
    """
    raw = status.get("pm")
    if raw is None:
        return None
    return round(raw / 10, 1)


def _air_quality(status: dict) -> str | None:
    return AIR_QUALITY_LABELS.get(status.get("airquality"))


@dataclass(frozen=True, kw_only=True)
class BeurerSensorDescription(SensorEntityDescription):
    """Describes a Beurer sensor."""

    value_fn: Callable[[dict], float | str | None]
    # Status field this sensor needs; the entity is skipped if the model lacks it.
    requires: str


SENSORS: tuple[BeurerSensorDescription, ...] = (
    BeurerSensorDescription(
        key="pm25",
        translation_key="pm25",
        requires="pm",
        device_class=SensorDeviceClass.PM25,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=MICROGRAMS_PER_CUBIC_METER,
        suggested_display_precision=1,
        value_fn=_pm25,
    ),
    BeurerSensorDescription(
        key="air_quality",
        translation_key="air_quality",
        requires="airquality",
        device_class=SensorDeviceClass.ENUM,
        options=list(AIR_QUALITY_LABELS.values()),
        value_fn=_air_quality,
    ),
    BeurerSensorDescription(
        key="humidity",
        translation_key="humidity",
        requires="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        value_fn=lambda s: s.get("humidity"),
    ),
    BeurerSensorDescription(
        key="temperature",
        translation_key="temperature",
        requires="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=_temperature,
    ),
    BeurerSensorDescription(
        key="fan_speed",
        translation_key="fan_speed",
        requires="fan",
        state_class=SensorStateClass.MEASUREMENT,
        # Numeric rather than the label, so it graphs and compares. 0 means off.
        value_fn=lambda s: s.get("fan"),
    ),
    BeurerSensorDescription(
        key="filter_left",
        translation_key="filter_left",
        requires="filterLeft",
        native_unit_of_measurement=UnitOfTime.HOURS,
        value_fn=lambda s: s.get("filterLeft"),
    ),
    BeurerSensorDescription(
        key="timer",
        translation_key="timer",
        requires="timerMin",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        value_fn=lambda s: s.get("timerMin"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BeurerConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities(
        BeurerSensor(coordinator, description)
        for coordinator in entry.runtime_data.coordinators
        for description in SENSORS
        if coordinator.supports(description.requires)
    )


class BeurerSensor(BeurerEntity, SensorEntity):
    """A value read from the pushed status frame."""

    entity_description: BeurerSensorDescription

    def __init__(self, coordinator, description: BeurerSensorDescription):
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self):
        return self.entity_description.value_fn(self.status)
