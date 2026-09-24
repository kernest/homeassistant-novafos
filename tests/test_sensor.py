from types import SimpleNamespace

from homeassistant.components.sensor import SensorStateClass

from custom_components.novafos.const import (
    EXTRA_HEATING_SENSOR_TYPES,
    HEATING_SENSOR_TYPES,
    WATER_SENSOR_TYPES,
)
from custom_components.novafos.sensor import NovafosWaterSensor


def make_sensor(data, description=WATER_SENSOR_TYPES[0], cumulative_totals=None):
    sensor = object.__new__(NovafosWaterSensor)
    sensor.coordinator = SimpleNamespace(
        data=data, cumulative_totals=cumulative_totals or {}
    )
    sensor.entity_description = description
    sensor._attrs = {}
    return sensor


def test_hourly_sensor_returns_latest_completed_reading():
    sensor = make_sensor(
        (
            {
                "water": [
                    {"DateFrom": "2026-09-22T21:00:00", "Value": 0.031},
                    {"DateFrom": "2026-09-22T22:00:00", "Value": 0.006},
                ]
            },
            None,
        )
    )

    assert sensor.native_value == 0.006
    assert sensor.extra_state_attributes == {"reading_start": "2026-09-22T22:00:00"}


def test_hourly_water_sensor_is_a_valid_interval_measurement():
    description = WATER_SENSOR_TYPES[0]

    assert description.device_class is None
    assert description.state_class is SensorStateClass.MEASUREMENT


def test_heating_sensor_metadata_matches_home_assistant_rules():
    hourly = HEATING_SENSOR_TYPES[0]
    statistics = HEATING_SENSOR_TYPES[1:] + EXTRA_HEATING_SENSOR_TYPES

    assert hourly.device_class is None
    assert hourly.state_class is SensorStateClass.MEASUREMENT
    assert all(sensor.state_class is SensorStateClass.TOTAL for sensor in statistics)


def test_hourly_sensor_handles_no_readings():
    sensor = make_sensor(({"water": []}, None))

    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


def test_sensor_handles_failed_first_refresh():
    sensor = make_sensor(None)

    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {}


def test_statistics_sensor_clears_missing_year_total():
    sensor = make_sensor(
        ({"water": []}, {"water": {"Data": []}}), WATER_SENSOR_TYPES[1]
    )
    sensor._attrs = {"year_total": 42.0}

    assert sensor.extra_state_attributes == {}


def test_statistics_sensor_has_no_state_but_exposes_total_attribute():
    """A state would make recorder write rows into the imported statistic."""
    sensor = make_sensor(
        ({"water": []}, {"water": {"Data": []}}),
        WATER_SENSOR_TYPES[1],
        {"water": 123.456},
    )

    assert sensor.native_value is None
    assert sensor.extra_state_attributes == {"cumulative_total": 123.456}


def test_meter_unit_follows_kmd_unit_for_heating():
    from homeassistant.components.sensor import SensorDeviceClass

    from custom_components.novafos.const import meter_unit

    def heating(name):
        return meter_unit({"type": "heating", "Unit": {"Name": name}})

    assert heating("MWh") == ("MWh", "energy", SensorDeviceClass.ENERGY)
    assert heating("kWh") == ("kWh", "energy", SensorDeviceClass.ENERGY)
    assert heating("GJ") == ("GJ", "energy", SensorDeviceClass.ENERGY)
    assert heating("m³") == ("m³", "volume", SensorDeviceClass.VOLUME)
    # Unknown units keep the previous default.
    assert heating("?") == ("kWh", "energy", SensorDeviceClass.ENERGY)
    assert meter_unit({"type": "water", "Unit": {"Name": "m³"}}) == (
        "m³",
        "volume",
        SensorDeviceClass.WATER,
    )
