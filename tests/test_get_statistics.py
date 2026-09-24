# import pytest
import requests
from datetime import datetime, timedelta
import tests.utils
from custom_components.novafos.pynovafos.novafos import Novafos


# Test cases:
# _get_statistics(self, days_back = None, from_date = None)
# get_statistics(self, from_date = None):
#   1)  from_date = None
#   3)  from_date = "<date in month>"


def prepare_active_meter(mocker, novafos):
    mock_post = mocker.patch("requests.post")
    mock_response = requests.Response()
    mock_response.status_code = 200
    mock_response._content = tests.utils.load_data("active_meters_water.json")
    mock_post.return_value = mock_response
    novafos._get_active_meters()


def prepare_active_meters(mocker, novafos):
    mock_post = mocker.patch("requests.post")
    mock_response = requests.Response()
    mock_response.status_code = 200
    mock_response._content = tests.utils.load_data(
        "active_meters_water_and_heating.json"
    )
    mock_post.return_value = mock_response
    novafos._get_active_meters()


# -----------------------------
# @pytest.mark.skip(reason="Skipped")
def test_get_statistics_single(mocker, novafos):
    prepare_active_meter(mocker, novafos)

    mock_post = mocker.patch("requests.post")
    mock_response_1 = requests.Response()
    mock_response_1.status_code = 200
    mock_response_1._content = tests.utils.load_data(
        "consumption_hour_data_water_zoom_3.json"
    )

    mock_response_2 = requests.Response()
    mock_response_2.status_code = 200
    mock_response_2._content = tests.utils.load_data(
        "consumption_hour_data_heating_zoom_3.json"
    )

    mock_post.side_effect = [mock_response_1, mock_response_2]

    assert novafos.get_statistics(from_date=None) == {}


# @pytest.mark.skip(reason="Skipped")
def test_statistics(mocker, data_regression, novafos) -> None:
    prepare_active_meter(mocker, novafos)

    mock_post = mocker.patch("requests.post")
    mock_response_1 = requests.Response()
    mock_response_1.status_code = 200
    mock_response_1._content = tests.utils.load_data(
        "consumption_hour_data_water_zoom_3.json"
    )

    mock_response_2 = requests.Response()
    mock_response_2.status_code = 200
    mock_response_2._content = tests.utils.load_data(
        "consumption_hour_data_heating_zoom_3.json"
    )

    mock_post.side_effect = [mock_response_1, mock_response_2]

    from_date = datetime.now() - timedelta(days=1)
    novafos.get_statistics(from_date=from_date)
    data_regression.check(novafos._meter_data)


def _hourly_response(dateFrom, dateTo, max_hours=None):
    """Build a KMD-like response with one point per hour in the UTC range."""
    start = datetime.fromisoformat(dateFrom.replace("Z", "+00:00"))
    stop = datetime.fromisoformat(dateTo.replace("Z", "+00:00"))
    hours = []
    point = start
    while point < stop and (max_hours is None or len(hours) < max_hours):
        hours.append(point.isoformat())
        point += timedelta(hours=1)
    return [
        {
            "type": meter_type,
            "Data": [{"DateFrom": hour, "Value": value} for hour in hours],
            "Extra": {
                "Sum": value * len(hours),
                "Avg": value,
                "Max": value,
                "Min": value,
                "LastValidDate": dateTo,
            },
        }
        for meter_type, value in (("water", 1.0), ("heating", 2.0))
    ]


def _zero_after_first_day(dateFrom, dateTo):
    """KMD returning every hour, but zeros after the first day."""
    series = _hourly_response(dateFrom, dateTo)
    for meter in series:
        for index, point in enumerate(meter["Data"]):
            if index >= 24:
                point["Value"] = 0.0
        meter["Extra"]["Sum"] = len(meter["Data"]) * (
            1.0 if meter["type"] == "water" else 2.0
        )
    return series


def _chunk_api():
    api = Novafos(timezone="Europe/Copenhagen", chunk_days=31)
    api._meter_data = {"water": [], "heating": []}
    api._meter_data_extra = {"water": [], "heating": []}
    return api


def test_statistics_uses_chunks_and_keeps_meter_types_separate(mocker) -> None:
    api = _chunk_api()
    fetch = mocker.patch.object(
        api,
        "_get_all_consumption_timeseries",
        side_effect=lambda dateFrom, dateTo, zoomLevel: _hourly_response(
            dateFrom, dateTo
        ),
    )

    api.get_statistics(from_date=datetime.now() - timedelta(days=65))

    assert fetch.call_count == 3
    assert {point["Value"] for point in api._meter_data["water"]} == {1.0}
    assert {point["Value"] for point in api._meter_data["heating"]} == {2.0}
    # 65 days of hours, give or take a daylight saving time change.
    assert abs(len(api._meter_data["water"]) - 65 * 24) <= 1


def test_statistics_falls_back_to_daily_when_kmd_truncates(mocker) -> None:
    """KMD returning one day per multi-day request must not lose history."""
    api = _chunk_api()
    fetch = mocker.patch.object(
        api,
        "_get_all_consumption_timeseries",
        side_effect=lambda dateFrom, dateTo, zoomLevel: _hourly_response(
            dateFrom, dateTo, max_hours=24
        ),
    )

    api.get_statistics(from_date=datetime.now() - timedelta(days=65))

    assert abs(len(api._meter_data["water"]) - 65 * 24) <= 1
    assert abs(len(api._meter_data["heating"]) - 65 * 24) <= 1
    dates = [point["DateFrom"] for point in api._meter_data["water"]]
    assert len(dates) == len(set(dates))
    # One truncated chunk, its 31 daily re-fetches, then 34 daily requests.
    assert fetch.call_count == 1 + 31 + 34


def test_get_statistics_replaces_previous_snapshot(mocker) -> None:
    """Repeated coordinator requests must not duplicate hourly points."""
    api = Novafos(timezone="Europe/Copenhagen", chunk_days=31)
    api._active_meters = [
        {
            "type": "water",
            "InstallationId": 1,
            "MeasurementPointId": 2,
            "Unit": {"Id": 3},
        }
    ]
    api._meter_data = {"water": [{"DateFrom": "old", "Value": 99.0}]}
    api._meter_data_extra = {"water": [{"Sum": 99.0}]}
    mocker.patch.object(
        api,
        "_get_all_consumption_timeseries",
        return_value=[
            {
                "type": "water",
                "Data": [{"DateFrom": "2026-09-22T00:00:00", "Value": 0.1}],
                "Extra": {
                    "Sum": 0.1,
                    "Avg": 0.1,
                    "Min": 0.1,
                    "Max": 0.1,
                    "LastValidDate": "2026-09-22T00:59:59",
                },
            }
        ],
    )

    from_date = datetime.now() - timedelta(days=1)
    first = api.get_statistics(from_date=from_date)
    second = api.get_statistics(from_date=from_date)

    expected = {"water": [{"DateFrom": "2026-09-22T00:00:00", "Value": 0.1}]}
    assert first == expected
    assert second == expected


def test_statistics_falls_back_when_hourly_values_miss_the_total(mocker) -> None:
    """Hourly zeros that disagree with KMD's own total trigger daily fetches."""
    api = _chunk_api()
    mocker.patch.object(
        api,
        "_get_all_consumption_timeseries",
        side_effect=lambda dateFrom, dateTo, zoomLevel: _zero_after_first_day(
            dateFrom, dateTo
        ),
    )

    api.get_statistics(from_date=datetime.now() - timedelta(days=65))

    water = api._meter_data["water"]
    assert abs(len(water) - 65 * 24) <= 1
    assert all(point["Value"] == 1.0 for point in water)
