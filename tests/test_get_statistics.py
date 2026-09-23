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


def test_statistics_uses_chunks_and_keeps_meter_types_separate(mocker) -> None:
    api = Novafos(timezone="Europe/Copenhagen", chunk_days=31)
    api._meter_data = {"water": [], "heating": []}
    api._meter_data_extra = {"water": [], "heating": []}

    def chunk_response(dateFrom, dateTo, zoomLevel):
        extra = {
            "Sum": 1.0,
            "Avg": 1.0,
            "Max": 1.0,
            "Min": 1.0,
            "LastValidDate": dateTo,
        }
        return [
            {
                "type": "water",
                "Data": [{"DateFrom": dateFrom, "Value": 1.0}],
                "Extra": extra,
            },
            {
                "type": "heating",
                "Data": [{"DateFrom": dateFrom, "Value": 2.0}],
                "Extra": extra,
            },
        ]

    fetch = mocker.patch.object(
        api, "_get_all_consumption_timeseries", side_effect=chunk_response
    )

    api.get_statistics(from_date=datetime.now() - timedelta(days=65))

    assert fetch.call_count == 3
    assert [point["Value"] for point in api._meter_data["water"]] == [1.0] * 3
    assert [point["Value"] for point in api._meter_data["heating"]] == [2.0] * 3
