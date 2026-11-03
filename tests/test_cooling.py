"""Cooling report and extra meter unit tests."""

import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import requests

from homeassistant.components.recorder.models import StatisticMeanType
from homeassistant.components.sensor import SensorDeviceClass

from custom_components.novafos import Novafos
from custom_components.novafos import coordinator as coordinator_module
from custom_components.novafos.const import COOLING_SENSOR_TYPES, detail_unit
from custom_components.novafos.coordinator import NovafosUpdateCoordinator
from custom_components.novafos.pynovafos.novafos import HTTPFailed
from custom_components.novafos.sensor import NovafosCoolingSensor

HEAT_METER = {
    "type": "heating",
    "InstallationId": 1,
    "MeasurementPointId": 2,
    "MeterTypeId": 3,
    "Unit": {"Id": 1885, "Name": "MWh", "Description": "Megawatt timer"},
    "Units": [
        {"Id": 1885, "Name": "MWh", "Description": "Megawatt timer"},
        {"Id": 16201403, "Name": "MWh-F", "Description": "MWh fremløb"},
    ],
}


def cooling_row(period, flow=0.2, supply=58.0, ret=30.0):
    return {
        "Period": period,
        "Flow": flow,
        "DegreesIn": supply,
        "DegreesOut": ret,
        "Degrees": supply - ret,
        "DegreesAccumulated": 36.5,
        "DegreesPenalty": 44.9,
        "DegreesReimbursement": 44.9,
        "Energy": 0.002,
    }


EMPTY_ROW = {
    "Period": "23",
    "Flow": 0.0,
    "DegreesIn": "NaN",
    "DegreesOut": "NaN",
    "Degrees": "NaN",
    "DegreesAccumulated": None,
    "DegreesPenalty": None,
    "DegreesReimbursement": None,
    "Energy": None,
}


def response(payload):
    resp = requests.Response()
    resp.status_code = 200
    resp._content = json.dumps(payload).encode()
    return resp


@pytest.fixture
def api():
    instance = Novafos(timezone="Europe/Copenhagen")
    instance._active_meters = [HEAT_METER]
    return instance


def test_cooling_values_turn_nan_into_none():
    values = Novafos._cooling_values(EMPTY_ROW)
    assert values["supply_temperature"] is None
    assert values["return_requirement"] is None
    assert values["volume"] == 0.0


def test_cooling_info_request(mocker, api):
    post = mocker.patch(
        "requests.post",
        return_value=response({"CoolingData": [cooling_row("0")], "IsSuccess": True}),
    )
    rows = api._get_cooling_info(
        HEAT_METER, 4, datetime(2026, 10, 29), datetime(2026, 10, 30)
    )
    assert rows[0]["Period"] == "0"
    body = post.call_args.kwargs["json"]
    assert post.call_args.args[0].endswith("/api/cooling/getcoolinginfo")
    assert body == {
        "MetertypeId": 3,
        "MeasurementPointId": 2,
        "ZoomLevel": 4,
        "DateFrom": "2026-10-28T23:00:00Z",
        "DateTo": "2026-10-29T22:59:59Z",
    }


def test_cooling_info_reports_kmd_errors(mocker, api):
    mocker.patch(
        "requests.post",
        return_value=response(
            {"CoolingData": None, "IsSuccess": False, "ErrorMessage": "x"}
        ),
    )
    with pytest.raises(HTTPFailed):
        api._get_cooling_info(
            HEAT_METER, 4, datetime(2026, 10, 29), datetime(2026, 10, 30)
        )


def test_cooling_hourly_fetches_one_day_per_request(mocker, api):
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    get = mocker.patch.object(
        api,
        "_get_cooling_info",
        side_effect=lambda meter, zoom, start, stop: [
            cooling_row("0", ret=31.0),
            cooling_row("1", ret=32.0),
            EMPTY_ROW,
        ],
    )
    rows = api.get_cooling_hourly(today - timedelta(days=2))
    assert get.call_count == 2
    assert all(call.args[1] == 4 for call in get.call_args_list)
    assert all(
        call.args[3] - call.args[2] == timedelta(days=1) for call in get.call_args_list
    )
    # The hour KMD has no reading for is left out.
    assert [row["DateFrom"] for row in rows] == [
        (today - timedelta(days=2)).isoformat(),
        (today - timedelta(days=2, hours=-1)).isoformat(),
        (today - timedelta(days=1)).isoformat(),
        (today - timedelta(days=1, hours=-1)).isoformat(),
    ]
    assert rows[1]["return_temperature"] == 32.0
    assert rows[1]["return_requirement"] == 44.9


def test_cooling_hourly_skips_low_flow_hours(mocker, api):
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    mocker.patch.object(
        api,
        "_get_cooling_info",
        return_value=[
            cooling_row("0", flow=0.002, ret=430.0),
            cooling_row("1", flow=0.004, ret=0.0),
            cooling_row("2", flow=0.1, ret=33.0),
        ],
    )
    rows = api.get_cooling_hourly(today - timedelta(days=1))
    assert [row["return_temperature"] for row in rows] == [33.0]


def test_cooling_summary_falls_back_to_previous_month(mocker, api):
    now = datetime.now()

    def report(meter, zoom, start, stop):
        if zoom == 1:
            return [
                {**cooling_row(str(now.year - 1))},
                {**cooling_row(str(now.year), ret=36.5), "DegreesAccumulated": None},
            ]
        if start.month == now.month and start.year == now.year:
            return [{**EMPTY_ROW, "Period": "1"}]
        return [
            cooling_row("1"),
            cooling_row("2", ret=33.0),
            {**EMPTY_ROW, "Period": "3"},
        ]

    mocker.patch.object(api, "_get_cooling_info", side_effect=report)
    summary = api.get_cooling_summary()
    assert summary["year"]["return_temperature"] == 36.5
    # The year row has no DegreesAccumulated; its return is the year's.
    assert summary["year"]["return_temperature_year"] == 36.5
    previous_month = (now.replace(day=1) - timedelta(days=1)).replace(day=2)
    assert summary["day"]["date"] == previous_month.date().isoformat()
    assert summary["day"]["return_temperature"] == 33.0


def test_detail_units_combine_named_and_listed_units(mocker, api):
    api._time_series_periods = [
        {"MeasurementPointId": 2, "UnitId": 1883, "RangeType": 0},
        {"MeasurementPointId": 2, "UnitId": 1885, "RangeType": 0},
        {"MeasurementPointId": 2, "UnitId": 16201404, "RangeType": 0},
        {"MeasurementPointId": 99, "UnitId": 1938, "RangeType": 0},
        {"MeasurementPointId": 0, "UnitId": 0, "RangeType": 1},
    ]
    units = api.get_detail_units("heating")
    assert [unit["Id"] for unit in units] == [1883, 16201403, 16201404]
    assert units[1]["Name"] == "MWh-F"
    assert units[0]["Name"] == ""
    assert api.get_detail_units("water") == []


def test_unit_statistics_requests_the_given_unit(mocker, api):
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    get = mocker.patch.object(
        api,
        "_get_consumption_timeseries",
        return_value={"Data": [{"DateFrom": "2026-10-29T00:00:00", "Value": 0.08}]},
    )
    unit = {"Id": 1883, "Name": ""}
    points = api.get_unit_statistics("heating", unit, today - timedelta(days=3))
    assert len(points) == 3
    assert all(call.args[0]["Unit"] == unit for call in get.call_args_list)
    assert get.call_args_list[0].args[0]["MeasurementPointId"] == 2


def test_detail_unit_names():
    assert detail_unit({"Id": 1883}) == ("volume", "m³", "volume")
    assert detail_unit({"Id": 16201404})[0] == "return_energy"
    assert detail_unit({"Id": 5, "Name": "kWh-X"}) == ("unit_5", "kWh-X", None)
    assert detail_unit({"Id": 6, "Name": ""}) == ("unit_6", None, None)


def make_coordinator(monkeypatch, api, sums):
    async def run_executor(function, *args):
        return function(*args)

    hass = SimpleNamespace(
        config=SimpleNamespace(time_zone="Europe/Copenhagen"),
        async_add_executor_job=run_executor,
    )
    monkeypatch.setattr(
        coordinator_module,
        "get_instance",
        Mock(return_value=SimpleNamespace(async_add_executor_job=run_executor)),
    )
    monkeypatch.setattr(
        coordinator_module, "get_last_statistics", Mock(return_value={"x": [{}]})
    )
    added = Mock()
    monkeypatch.setattr(coordinator_module, "async_add_external_statistics", added)
    coordinator = SimpleNamespace(
        hass=hass,
        api=api,
        _details_reconciled=False,
        cooling_summary=None,
        _schedule_full_history=Mock(),
        _clear_unfiltered_cooling=AsyncMock(return_value=False),
        _sum_before=AsyncMock(
            side_effect=lambda statistic_id, point: sums.get(statistic_id)
        ),
    )
    coordinator._import_detail_unit = (
        lambda *args: NovafosUpdateCoordinator._import_detail_unit(coordinator, *args)
    )
    return coordinator, added


def fake_api():
    api = Mock()
    api.get_meter_types.return_value = [{"type": "heating"}]
    api.get_detail_units.return_value = [{"Id": 1883, "Name": "", "Description": ""}]
    api.get_unit_statistics.return_value = [
        {"DateFrom": "2026-10-29T00:00:00", "Value": 0.08},
        {"DateFrom": "2026-10-29T01:00:00", "Value": None},
        {"DateFrom": "2026-10-29T02:00:00", "Value": 0.07},
    ]
    api.get_cooling_hourly.return_value = [
        {
            "DateFrom": "2026-10-29T00:00:00",
            "supply_temperature": 53.8,
            "return_temperature": 32.2,
            "cooling": 32.2,
            "return_temperature_year": 36.6,
            "return_requirement": 44.9,
        },
        {
            "DateFrom": "2026-10-29T01:00:00",
            "supply_temperature": 56.6,
            "return_temperature": None,
            "cooling": 22.6,
            "return_temperature_year": 36.6,
            "return_requirement": 44.9,
        },
    ]
    api.get_cooling_summary.return_value = {
        "year": {"return_temperature": 36.5},
        "day": None,
    }
    return api


@pytest.mark.asyncio
async def test_meter_details_continue_the_stored_sum(monkeypatch):
    api = fake_api()
    coordinator, added = make_coordinator(
        monkeypatch, api, {"novafos:heating_volume": 100.0}
    )
    await NovafosUpdateCoordinator._import_meter_details(
        coordinator, datetime(2026, 10, 2), False, {}
    )

    calls = {call.args[1]["statistic_id"]: call.args for call in added.call_args_list}
    metadata, rows = calls["novafos:heating_volume"][1:]
    assert metadata["has_sum"] is True
    assert metadata["unit_of_measurement"] == "m³"
    assert metadata["unit_class"] == "volume"
    assert [row["sum"] for row in rows] == pytest.approx([100.08, 100.08, 100.15])
    assert rows[0]["start"].utcoffset() == timedelta(hours=1)

    metadata, rows = calls["novafos:heating_return_temperature"][1:]
    assert metadata["mean_type"] is StatisticMeanType.ARITHMETIC
    assert metadata["has_sum"] is False
    assert metadata["unit_class"] == "temperature"
    # The hour without a return temperature is left out of that statistic.
    assert [row["mean"] for row in rows] == [32.2]
    assert len(calls["novafos:heating_supply_temperature"][2]) == 2
    assert coordinator.cooling_summary == {
        "year": {"return_temperature": 36.5},
        "day": None,
    }
    coordinator._schedule_full_history.assert_not_called()


@pytest.mark.asyncio
async def test_missing_detail_history_schedules_one_full_import(monkeypatch):
    api = fake_api()
    coordinator, added = make_coordinator(monkeypatch, api, {})
    await NovafosUpdateCoordinator._import_meter_details(
        coordinator, datetime(2026, 10, 2), False, {}
    )
    await NovafosUpdateCoordinator._import_meter_details(
        coordinator, datetime(2026, 10, 2), False, {}
    )
    coordinator._schedule_full_history.assert_called_once()
    api.get_unit_statistics.assert_not_called()
    assert "novafos:heating_volume" not in {
        call.args[1]["statistic_id"] for call in added.call_args_list
    }

    # The full import itself starts the sum at zero.
    await NovafosUpdateCoordinator._import_meter_details(
        coordinator, datetime(2026, 1, 1), True, {}
    )
    rows = next(
        call.args[2]
        for call in added.call_args_list
        if call.args[1]["statistic_id"] == "novafos:heating_volume"
    )
    assert rows[-1]["sum"] == pytest.approx(0.15)


@pytest.mark.asyncio
async def test_meter_detail_failures_do_not_raise(monkeypatch):
    api = fake_api()
    api.get_detail_units.side_effect = HTTPFailed()
    api.get_cooling_hourly.side_effect = HTTPFailed()
    coordinator, added = make_coordinator(monkeypatch, api, {})
    await NovafosUpdateCoordinator._import_meter_details(
        coordinator, datetime(2026, 10, 2), False, {}
    )
    added.assert_not_called()


def make_cooling_sensor(key, summary):
    description = next(d for d in COOLING_SENSOR_TYPES if d.key == key)
    sensor = object.__new__(NovafosCoolingSensor)
    sensor.coordinator = SimpleNamespace(cooling_summary=summary)
    sensor.entity_description = description
    return sensor


def test_cooling_sensors():
    summary = {
        "year": {
            "return_temperature": 36.5,
            "return_requirement": 44.9,
            "supply_temperature": 60.1,
        },
        "day": {"return_temperature": 30.1, "date": "2026-10-31"},
    }
    margin = make_cooling_sensor("cooling_year_return_margin", summary)
    assert margin.native_value == 8.4
    assert margin.extra_state_attributes == {}
    day = make_cooling_sensor("cooling_day_return_temperature", summary)
    assert day.native_value == 30.1
    assert day.extra_state_attributes == {"date": "2026-10-31"}
    assert day.entity_description.device_class is SensorDeviceClass.TEMPERATURE
    assert (
        make_cooling_sensor(
            "cooling_day_volume", {"year": None, "day": None}
        ).native_value
        is None
    )
    assert make_cooling_sensor("cooling_year_return_margin", None).native_value is None


@pytest.mark.asyncio
async def test_unfiltered_cooling_statistics_are_cleared_once(monkeypatch):
    saved = {}

    class FakeStore:
        def __init__(self, hass, version, key):
            self.key = key

        async def async_load(self):
            return saved.get(self.key)

        async def async_save(self, data):
            saved[self.key] = data

    recorder = Mock()
    monkeypatch.setattr(coordinator_module, "Store", FakeStore)
    monkeypatch.setattr(coordinator_module, "get_instance", Mock(return_value=recorder))

    def run(coordinator):
        return NovafosUpdateCoordinator._clear_unfiltered_cooling(coordinator)

    first = SimpleNamespace(
        hass=None, entry=SimpleNamespace(entry_id="e1"), _cooling_filter_checked=False
    )
    assert await run(first) is True
    assert await run(first) is False
    recorder.async_clear_statistics.assert_called_once()
    cleared = recorder.async_clear_statistics.call_args.args[0]
    assert "novafos:heating_return_temperature" in cleared
    assert "novafos:heating_volume" not in cleared

    # A later start finds the stored filter version and keeps the statistics.
    later = SimpleNamespace(
        hass=None, entry=SimpleNamespace(entry_id="e1"), _cooling_filter_checked=False
    )
    assert await run(later) is False
    recorder.async_clear_statistics.assert_called_once()


@pytest.mark.asyncio
async def test_cleared_cooling_schedules_a_full_import(monkeypatch):
    api = fake_api()
    coordinator, added = make_coordinator(
        monkeypatch, api, {"novafos:heating_volume": 100.0}
    )
    coordinator._clear_unfiltered_cooling = AsyncMock(return_value=True)
    await NovafosUpdateCoordinator._import_meter_details(
        coordinator, datetime(2026, 10, 2), False, {}
    )
    coordinator._schedule_full_history.assert_called_once()
