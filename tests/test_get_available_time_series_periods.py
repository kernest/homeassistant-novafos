import json

import requests


def mock_periods_response(mocker, periods):
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps(periods).encode()
    mocker.patch("requests.get", return_value=response)


def test_available_period_uses_meter_range(mocker, novafos):
    mock_periods_response(
        mocker,
        [
            {"RangeType": 1, "MinDate": "2020-01-01T00:00:00"},
            {"RangeType": 0, "MinDate": "2024-05-01T00:00:00"},
            {"RangeType": 0, "MinDate": "2023-08-01T00:00:00"},
        ],
    )

    result = novafos.get_available_time_series_periods()

    assert result.isoformat() == "2023-08-01T00:00:00"


def test_available_period_handles_empty_response(mocker, novafos):
    mock_periods_response(mocker, [])

    assert novafos.get_available_time_series_periods() is None
