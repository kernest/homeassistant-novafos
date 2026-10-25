# Running tests (development)
If Home Assistant is installed as stand-alone using pip install and a venv:

```
source /srv/homeassistant/bin/activate

cd ~/.homeassistant/custom_componts/homeassistant-novafos
scripts/setup        # installs requirements_common.txt and requirements_dev.txt
```

The dev container runs `scripts/setup` automatically.

## Lint

```
ruff check .
ruff format --check .
```

## Run tests
From the repository root (`pytest.ini` sets the test path and asyncio mode):

```
pytest
```

The tests never contact KMD or a live Home Assistant: API responses are
mocked and the coordinator tests use an in-memory statistics store, plus one
round trip through a temporary recorder database.

## Commit code
```
git commit -a --no-verify
```


# pytest tips

```
async def test_update_data_success(mocker, hass):  # mocker is injected by pytest-mock
    mock_get = mocker.patch("custom_components.your_integration.requests.get")
    mock_response = requests.Response()
    mock_response.status_code = 200
    mock_response._content = b'{"key1": "value1", "key2": 123}'
    mock_get.return_value = mock_response
    data = await async_update_data(hass)
    assert data["key1"] == "value1"
    assert data["key2"] == 123
    mock_get.assert_called_once()

async def test_update_data_error(mocker, hass):
    mock_get = mocker.patch("custom_components.your_integration.requests.get")
    mock_get.side_effect = requests.exceptions.RequestException("API Error")
    with pytest.raises(requests.exceptions.RequestException):
        await async_update_data(hass)
```

## Skip a test:
```
import pytest

@pytest.mark.skip(reason="Need some data to test what is necessary here.")
```

## Mocking datetime

Use the `freezer` fixture from pytest-homeassistant-custom-component:

```
def test_something(freezer):
    freezer.move_to("2026-10-08T12:00:00")
    ... more test code
```
