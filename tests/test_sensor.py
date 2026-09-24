from types import SimpleNamespace

from custom_components.novafos.const import WATER_SENSOR_TYPES
from custom_components.novafos.sensor import NovafosWaterSensor


def make_sensor(data, description=WATER_SENSOR_TYPES[0]):
    sensor = object.__new__(NovafosWaterSensor)
    sensor.coordinator = SimpleNamespace(data=data)
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
