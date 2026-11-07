"""Statistics import tests for the Novafos coordinator."""

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.recorder import statistics as recorder_statistics
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import valid_statistic_id
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.novafos import coordinator as coordinator_module
from custom_components.novafos import update_listener
from custom_components.novafos.config_flow import OptionsFlowHandler
from custom_components.novafos.const import DOMAIN
from custom_components.novafos.coordinator import (
    NovafosUpdateCoordinator,
)
from custom_components.novafos.services import async_setup_services
from custom_components.novafos.sensor import async_setup_entry as async_setup_sensors


@pytest.mark.asyncio
async def test_saved_grouped_setting_creates_only_hourly_sensors():
    coordinator = Mock()
    coordinator.data = ({"water": [], "heating": []}, None)
    coordinator.api.get_meter_types.return_value = []
    entry = SimpleNamespace(
        entry_id="entry-id",
        data={"name": "Novafos", "use_grouped_sensors": True},
        options={"use_grouped_sensors": True},
    )
    hass = SimpleNamespace(
        data={DOMAIN: {entry.entry_id: {"coordinator": coordinator}}}
    )
    add_entities = Mock()
    await async_setup_sensors(hass, entry, add_entities)
    entities = add_entities.call_args.args[0]
    keys = {entity.entity_description.key for entity in entities}
    # This fork keeps the hourly sensors and adds cooling sensors; the
    # grouped day/week/month/year sensors are gone.
    assert {"hourly", "statistics"} <= keys
    assert not keys & {
        "statistics_day",
        "statistics_week",
        "statistics_month",
        "statistic_years",
        "statistics_year",
    }
    assert {
        "novafos-water-statistics",
        "novafos-heating-statistics",
    } <= {entity.unique_id for entity in entities}


@pytest.mark.asyncio
async def test_four_instances_have_independent_statistics_and_sensors(monkeypatch):
    async def execute(function, *args):
        return function(*args)

    monkeypatch.setattr(
        coordinator_module,
        "get_instance",
        Mock(return_value=SimpleNamespace(async_add_executor_job=execute)),
    )
    monkeypatch.setattr(
        coordinator_module, "get_last_statistics", Mock(return_value={})
    )
    history = Mock(return_value={})
    monkeypatch.setattr(coordinator_module, "statistics_during_period", history)
    internal = Mock()
    external = Mock()
    monkeypatch.setattr(coordinator_module, "async_import_statistics", internal)
    monkeypatch.setattr(coordinator_module, "async_add_external_statistics", external)
    entity_ids = set()
    unique_ids = set()
    for customer_id in ("11", "22"):
        for meter_type, point_id in (("water", "101"), ("heating", "102")):
            entry = SimpleNamespace(
                entry_id=f"{customer_id}_{point_id}",
                options={},
                data={
                    "name": "Novafos",
                    "customer_id": customer_id,
                    "measurement_point_id": point_id,
                    "meter_type": meter_type,
                },
            )
            api = Mock()
            api.get_meter_types.return_value = [{"type": meter_type}]
            api.get_available_time_series_periods.return_value = datetime(2026, 1, 1)
            api.get_statistics.return_value = {
                meter_type: [{"DateFrom": "2026-10-01T00:00:00", "Value": 1.0}]
            }
            hass = SimpleNamespace(
                config=SimpleNamespace(time_zone="Europe/Copenhagen"),
                async_add_executor_job=execute,
            )
            coordinator = SimpleNamespace(
                hass=hass,
                api=api,
                entry=entry,
                cumulative_totals={},
                _auto_reconciled=True,
                _sum_before=AsyncMock(return_value=0.0),
                _import_cost_statistics=AsyncMock(),
                _import_meter_details=AsyncMock(),
                _schedule_full_history=Mock(),
                _kmd_year_total=NovafosUpdateCoordinator._kmd_year_total,
            )
            fetched = await NovafosUpdateCoordinator._insert_statistics(
                coordinator, None, False
            )
            await NovafosUpdateCoordinator._insert_external_hourly_statistics(
                coordinator, fetched
            )
            entity_coordinator = Mock()
            entity_coordinator.data = ({meter_type: []}, None)
            entity_coordinator.api.get_meter_types.return_value = []
            hass.data = {DOMAIN: {entry.entry_id: {"coordinator": entity_coordinator}}}
            add_entities = Mock()
            await async_setup_sensors(hass, entry, add_entities)
            entity = next(
                entity
                for entity in add_entities.call_args.args[0]
                if entity.entity_description.key == "statistics"
            )
            expected = (
                f"sensor.novafos_{customer_id}_{point_id}_{meter_type}_statistics"
            )
            assert entity.entity_id == expected
            entity_ids.add(entity.entity_id)
            unique_ids.add(entity.unique_id)
            assert internal.call_args.args[1]["statistic_id"] == expected
            assert external.call_args.args[1]["statistic_id"] == (
                f"novafos:{customer_id}_{point_id}_{meter_type}_hourly_consumption"
            )
            assert history.call_args.args[3] == {expected}
    assert len(entity_ids) == len(unique_ids) == 4
    # Each instance writes its consumption and hourly external statistics.
    external_ids = {call.args[1]["statistic_id"] for call in external.call_args_list}
    assert len(external_ids) == external.call_count == 8
    assert "novafos:22_102_heating_consumption" in external_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [None, "entry-1"])
async def test_token_service_refreshes_all_or_selected_entry(target):
    coordinators = {
        f"entry-{index}": SimpleNamespace(async_refresh=AsyncMock())
        for index in range(4)
    }
    services = Mock()
    services.has_service.return_value = False
    hass = SimpleNamespace(
        services=services,
        data={
            DOMAIN: {key: {"coordinator": value} for key, value in coordinators.items()}
        },
    )
    await async_setup_services(hass, coordinators["entry-0"])
    handler = services.async_register.call_args.args[2]
    data = {"access_token": "new-token", "access_token_date_updated": ""}
    if target is not None:
        data["config_entry_id"] = target
    await handler(SimpleNamespace(data=data))
    for entry_id, coordinator in coordinators.items():
        if target is None or entry_id == target:
            assert coordinator.access_token == "new-token"
            assert coordinator.access_token_date_updated
            coordinator.async_refresh.assert_awaited_once()
        else:
            coordinator.async_refresh.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("authenticated", [True, False])
@pytest.mark.parametrize(
    ("data", "options", "external"),
    [
        ({"use_grouped_sensors": True}, {}, True),
        (
            {"use_grouped_sensors": True},
            {"use_external_statistics": False},
            False,
        ),
        (
            {"use_grouped_sensors": False},
            {"use_external_statistics": True},
            True,
        ),
        ({"use_grouped_sensors": True}, {"use_grouped_sensors": False}, True),
        (
            {"use_grouped_sensors": False, "use_external_statistics": False},
            {},
            False,
        ),
    ],
)
async def test_statistics_options_ignore_removed_grouped_setting(
    data, options, external, authenticated, monkeypatch
):
    async def run_executor(function, *args):
        return function(*args)

    recorder = SimpleNamespace(async_block_till_done=AsyncMock())
    monkeypatch.setattr(coordinator_module, "get_instance", Mock(return_value=recorder))
    api = Mock()
    api.authenticate_using_access_token.return_value = authenticated
    api.get_year_data.return_value = {}
    api._meter_data = {}
    coordinator = SimpleNamespace(
        hass=SimpleNamespace(async_add_executor_job=run_executor),
        api=api,
        entry=SimpleNamespace(data=data, options=options),
        access_token="",
        access_token_date_updated="",
        _import_lock=asyncio.Lock(),
        _insert_statistics=AsyncMock(return_value={"heating": []}),
        _insert_external_hourly_statistics=AsyncMock(),
    )

    if authenticated:
        await NovafosUpdateCoordinator._async_update_data(coordinator)
    else:
        # An invalid token fails the refresh before any import.
        with pytest.raises(UpdateFailed):
            await NovafosUpdateCoordinator._async_update_data(coordinator)

    assert coordinator._insert_statistics.await_count == int(authenticated)
    api.get_grouped_statistics.assert_not_called()
    external = external and authenticated
    assert coordinator._insert_external_hourly_statistics.await_count == int(external)
    recorder.async_block_till_done.assert_not_awaited()
    if external:
        coordinator._insert_external_hourly_statistics.assert_awaited_once_with(
            {"heating": []} if authenticated else None
        )


@pytest.mark.asyncio
async def test_configure_preserves_token_when_changing_statistics_options():
    entry = SimpleNamespace(
        data={"use_grouped_sensors": True},
        options={
            "access_token": "saved-token",
            "access_token_date_updated": "2026-09-01T00:00:00",
        },
    )
    flow = OptionsFlowHandler(entry)
    flow.async_show_form = Mock(side_effect=lambda **kwargs: kwargs)
    flow.async_create_entry = Mock(side_effect=lambda **kwargs: kwargs)

    form = await flow.async_step_init()
    defaults = form["data_schema"]({})
    assert "use_grouped_sensors" not in defaults
    assert defaults["use_external_statistics"] is True

    result = await flow.async_step_init({**defaults, "use_external_statistics": True})
    assert result["data"]["access_token"] == "saved-token"
    assert result["data"]["access_token_date_updated"] == "2026-09-01T00:00:00"
    assert "use_grouped_sensors" not in result["data"]


@pytest.mark.asyncio
async def test_removed_grouped_option_does_not_reload_entities():
    coordinator = SimpleNamespace(
        async_request_refresh=AsyncMock(),
    )
    hass = SimpleNamespace(
        data={DOMAIN: {"entry-id": {"coordinator": coordinator}}},
        config_entries=SimpleNamespace(async_reload=AsyncMock()),
    )
    entry = SimpleNamespace(
        entry_id="entry-id",
        data={"use_grouped_sensors": True},
        options={"use_external_statistics": False},
    )
    await update_listener(hass, entry)
    coordinator.async_request_refresh.assert_awaited_once()
    hass.config_entries.async_reload.assert_not_awaited()

    entry.options["use_grouped_sensors"] = False
    await update_listener(hass, entry)
    assert coordinator.async_request_refresh.await_count == 2
    hass.config_entries.async_reload.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("day", "hour_count"),
    [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)],
)
async def test_external_hourly_statistics_backfill_handles_dst(
    monkeypatch, day, hour_count
):
    async def run_executor(function, *args):
        return function(*args)

    timezone_local = ZoneInfo("Europe/Copenhagen")
    day_start = datetime.combine(day, datetime.min.time(), timezone_local)
    next_day_start = datetime.combine(
        day + timedelta(days=1), datetime.min.time(), timezone_local
    )
    start_utc = day_start.astimezone(timezone.utc)
    next_start_utc = next_day_start.astimezone(timezone.utc)
    assert (next_start_utc - start_utc) == timedelta(hours=hour_count)

    def hourly_rows(value):
        return [
            {
                "start": (start_utc + timedelta(hours=hour)).timestamp(),
                "state": value,
                "sum": round((hour + 1) * value, 3),
            }
            for hour in range(hour_count)
        ]

    history = {
        "sensor.novafos_water_statistics": hourly_rows(0.1),
        "sensor.novafos_heating_statistics": hourly_rows(2.0)[:-1],
    }
    hass = SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Copenhagen"))
    api = Mock()
    api.get_meter_types.return_value = [{"type": "water"}, {"type": "heating"}]
    api.get_meter_types.return_value = [{"type": "water"}, {"type": "heating"}]
    coordinator = SimpleNamespace(hass=hass, api=api, entry=SimpleNamespace(data={}))
    monkeypatch.setattr(
        coordinator_module,
        "get_instance",
        Mock(return_value=SimpleNamespace(async_add_executor_job=run_executor)),
    )
    monkeypatch.setattr(
        coordinator_module,
        "get_last_statistics",
        Mock(return_value={}),
    )
    query = Mock(
        side_effect=lambda hass, start, end, ids, period, units, types: {
            statistic_id: history[statistic_id] for statistic_id in ids
        }
    )
    monkeypatch.setattr(coordinator_module, "statistics_during_period", query)
    recorder = Mock()
    monkeypatch.setattr(
        recorder_statistics, "get_instance", Mock(return_value=recorder)
    )
    monkeypatch.setattr(
        coordinator_module,
        "async_add_external_statistics",
        recorder_statistics.async_add_external_statistics,
    )

    await NovafosUpdateCoordinator._insert_external_hourly_statistics(coordinator)

    assert recorder.async_import_statistics.call_count == 2
    metadata, rows = recorder.async_import_statistics.call_args_list[0].args[:2]
    assert valid_statistic_id(metadata["statistic_id"])
    assert metadata["statistic_id"] == "novafos:water_hourly_consumption"
    assert metadata["source"] == "novafos"
    assert metadata["mean_type"] is StatisticMeanType.ARITHMETIC
    assert metadata["has_sum"] is True
    assert metadata["unit_class"] == "volume"
    assert len(rows) == hour_count
    assert rows[0] == {
        "start": start_utc,
        "min": 0.1,
        "max": 0.1,
        "mean": 0.1,
        "sum": 0.1,
    }
    assert rows[-1]["start"] == next_start_utc - timedelta(hours=1)
    assert rows[-1]["sum"] == round(hour_count * 0.1, 3)
    heating_metadata, heating_rows = recorder.async_import_statistics.call_args_list[
        1
    ].args[:2]
    assert heating_metadata["statistic_id"] == "novafos:heating_hourly_consumption"
    assert len(heating_rows) == hour_count - 1
    assert query.call_args_list[0].args[1].year == 1970
    assert all(call.args[4] == "hour" for call in query.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("authenticated", [True, False])
async def test_external_hourly_statistics_recorder_roundtrip(
    async_setup_recorder_instance, hass, authenticated
):
    recorder = await async_setup_recorder_instance(hass)
    start = datetime(2026, 9, 21, tzinfo=ZoneInfo(hass.config.time_zone)).astimezone(
        timezone.utc
    )
    hourly_id = "sensor.novafos_water_statistics"
    statistic_id = "novafos:water_hourly_consumption"
    hourly_metadata = StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=None,
        source="recorder",
        statistic_id=hourly_id,
        unit_class="volume",
        unit_of_measurement="m³",
    )
    recorder_statistics.async_import_statistics(
        hass,
        hourly_metadata,
        [
            StatisticData(
                start=start + timedelta(hours=hour),
                state=1.0 if hour < 24 else 2.0,
                sum=hour + 1 if hour < 24 else 24 + (hour - 23) * 2,
            )
            for hour in range(24 if authenticated else 48)
        ],
    )
    await hass.async_add_executor_job(recorder.block_till_done)

    api = Mock()
    api.get_meter_types.return_value = [{"type": "water"}, {"type": "heating"}]
    api.authenticate_using_access_token.return_value = authenticated
    api.get_dummy_data.return_value = {}
    api.get_year_data.return_value = {}
    api._meter_data = {}
    fetched = {
        "water": [
            StatisticData(
                start=start + timedelta(hours=hour),
                state=2.0,
                sum=24 + (hour - 23) * 2,
            )
            for hour in range(24, 48)
        ]
    }
    coordinator = SimpleNamespace(
        hass=hass,
        api=api,
        entry=SimpleNamespace(data={"use_grouped_sensors": False}, options={}),
        access_token="",
        access_token_date_updated="",
        _import_lock=asyncio.Lock(),
        _insert_statistics=AsyncMock(return_value=fetched),
    )

    async def insert_external(hourly_statistics=None):
        await NovafosUpdateCoordinator._insert_external_hourly_statistics(
            coordinator, hourly_statistics
        )

    coordinator._insert_external_hourly_statistics = insert_external
    if authenticated:
        await NovafosUpdateCoordinator._async_update_data(coordinator)
    else:
        # An invalid token fails the refresh in this fork, so the external
        # statistics are built from the recorded hourly data alone.
        with pytest.raises(UpdateFailed):
            await NovafosUpdateCoordinator._async_update_data(coordinator)
        await insert_external()
    await hass.async_add_executor_job(recorder.block_till_done)
    assert coordinator._insert_statistics.await_count == int(authenticated)
    api.get_statistics.assert_not_called()

    internal = await recorder.async_add_executor_job(
        recorder_statistics.statistics_during_period,
        hass,
        start,
        None,
        {hourly_id},
        "hour",
        None,
        {"state", "sum"},
    )
    assert len(internal[hourly_id]) == (24 if authenticated else 48)

    result = await hass.async_add_executor_job(
        recorder_statistics.statistics_during_period,
        hass,
        start,
        None,
        {statistic_id},
        "week",
        None,
        {"min", "mean", "max", "sum"},
    )
    assert result[statistic_id][0]["min"] == 1.0
    assert result[statistic_id][0]["mean"] == 1.5
    assert result[statistic_id][0]["max"] == 2.0
    assert result[statistic_id][0]["sum"] == 72.0

    for period in ("week", "month", "year"):
        aggregate = await hass.async_add_executor_job(
            recorder_statistics.statistics_during_period,
            hass,
            start,
            None,
            {statistic_id},
            period,
            None,
            {"min", "mean", "max", "sum", "change"},
        )
        assert len(aggregate[statistic_id]) == 1
        assert aggregate[statistic_id][0]["min"] == 1.0
        assert aggregate[statistic_id][0]["mean"] == 1.5
        assert aggregate[statistic_id][0]["max"] == 2.0
        assert aggregate[statistic_id][0]["sum"] == 72.0
        assert aggregate[statistic_id][0]["change"] == 72.0

    hourly = await hass.async_add_executor_job(
        recorder_statistics.statistics_during_period,
        hass,
        start,
        None,
        {statistic_id},
        "hour",
        None,
        {"min", "mean", "max", "sum"},
    )
    assert len(hourly[statistic_id]) == 48
    assert hourly[statistic_id][0]["min"] == 1.0
    assert hourly[statistic_id][-1]["max"] == 2.0
    assert hourly[statistic_id][-1]["sum"] == 72.0

    daily = await hass.async_add_executor_job(
        recorder_statistics.statistics_during_period,
        hass,
        start,
        None,
        {statistic_id},
        "day",
        None,
        {"min", "mean", "max", "sum", "change"},
    )
    assert [row["min"] for row in daily[statistic_id]] == [1.0, 2.0]
    assert [row["mean"] for row in daily[statistic_id]] == [1.0, 2.0]
    assert [row["max"] for row in daily[statistic_id]] == [1.0, 2.0]
    assert [row["sum"] for row in daily[statistic_id]] == [24.0, 72.0]
    assert [row["change"] for row in daily[statistic_id]] == [24.0, 48.0]
    assert daily[statistic_id][1]["change"] == 48.0

    recorder_statistics.async_import_statistics(
        hass,
        hourly_metadata,
        [StatisticData(start=start + timedelta(hours=47), state=2.5, sum=72.5)],
    )
    await hass.async_add_executor_job(recorder.block_till_done)
    await NovafosUpdateCoordinator._insert_external_hourly_statistics(coordinator)
    await hass.async_add_executor_job(recorder.block_till_done)
    corrected = await hass.async_add_executor_job(
        recorder_statistics.statistics_during_period,
        hass,
        start,
        None,
        {statistic_id},
        "week",
        None,
        {"min", "mean", "max", "sum"},
    )
    assert len(corrected[statistic_id]) == 1
    assert corrected[statistic_id][0]["min"] == 1.0
    assert corrected[statistic_id][0]["mean"] == pytest.approx(72.5 / 48)
    assert corrected[statistic_id][0]["max"] == 2.5
    assert corrected[statistic_id][0]["sum"] == 72.5


@pytest.mark.asyncio
async def test_external_consumption_trends_across_calendar_boundaries(
    async_setup_recorder_instance, hass
):
    recorder = await async_setup_recorder_instance(hass)
    start = datetime(2023, 12, 31, tzinfo=ZoneInfo(hass.config.time_zone)).astimezone(
        timezone.utc
    )
    hourly_id = "sensor.novafos_water_statistics"
    statistic_id = "novafos:water_hourly_consumption"
    recorder_statistics.async_import_statistics(
        hass,
        StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=None,
            source="recorder",
            statistic_id=hourly_id,
            unit_class="volume",
            unit_of_measurement="m³",
        ),
        [
            StatisticData(
                start=start + timedelta(hours=hour),
                state=1.0 if hour < 24 else 2.0,
                sum=hour + 1 if hour < 24 else 24 + (hour - 23) * 2,
            )
            for hour in range(48)
        ],
    )
    await hass.async_add_executor_job(recorder.block_till_done)
    coordinator = SimpleNamespace(
        hass=hass,
        api=Mock(
            get_meter_types=Mock(return_value=[{"type": "water"}, {"type": "heating"}])
        ),
        entry=SimpleNamespace(data={}),
    )
    await NovafosUpdateCoordinator._insert_external_hourly_statistics(coordinator)
    await hass.async_add_executor_job(recorder.block_till_done)

    for period in ("day", "week", "month", "year"):
        result = await hass.async_add_executor_job(
            recorder_statistics.statistics_during_period,
            hass,
            start,
            None,
            {statistic_id},
            period,
            None,
            {"change"},
        )
        assert [row["change"] for row in result[statistic_id]] == [24.0, 48.0]


@pytest.mark.asyncio
async def test_sum_before_reads_the_imported_unit(async_setup_recorder_instance, hass):
    """A heating sensor shown in kWh must not turn an MWh anchor into kWh."""
    recorder = await async_setup_recorder_instance(hass)
    start = datetime(2026, 9, 30, tzinfo=ZoneInfo(hass.config.time_zone)).astimezone(
        timezone.utc
    )
    statistic_id = "sensor.novafos_heating_statistics"
    hass.states.async_set(
        statistic_id,
        "11.28",
        {
            "unit_of_measurement": "kWh",
            "device_class": "energy",
            "state_class": "total",
        },
    )
    recorder_statistics.async_import_statistics(
        hass,
        StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=None,
            source="recorder",
            statistic_id=statistic_id,
            unit_class="energy",
            unit_of_measurement="MWh",
        ),
        [StatisticData(start=start, state=0.002, sum=11.28)],
    )
    await hass.async_add_executor_job(recorder.block_till_done)
    coordinator = SimpleNamespace(hass=hass)
    point = start + timedelta(hours=1)

    shown = await NovafosUpdateCoordinator._sum_before(coordinator, statistic_id, point)
    assert shown == pytest.approx(11280.0)
    imported = await NovafosUpdateCoordinator._sum_before(
        coordinator, statistic_id, point, {"energy": "MWh"}
    )
    assert imported == pytest.approx(11.28)


@pytest.mark.asyncio
async def test_token_service_survives_entries_changing_and_persists_token():
    """A refresh that adds/removes an entry must not break the loop (HTTP 500)."""
    store = {}

    async def refresh_and_mutate():
        store.pop("entry-1", None)
        store["late-entry"] = {"coordinator": SimpleNamespace(async_refresh=AsyncMock())}

    first = SimpleNamespace(async_refresh=AsyncMock(side_effect=refresh_and_mutate))
    second = SimpleNamespace(async_refresh=AsyncMock())
    store.update({"entry-0": {"coordinator": first}, "entry-1": {"coordinator": second}})
    entries = {
        key: SimpleNamespace(entry_id=key, options={"use_grouped_sensors": False})
        for key in ("entry-0", "entry-1")
    }
    config_entries = Mock()
    config_entries.async_get_entry.side_effect = entries.get
    services = Mock()
    services.has_service.return_value = False
    hass = SimpleNamespace(services=services, config_entries=config_entries, data={DOMAIN: store})
    await async_setup_services(hass, first)
    handler = services.async_register.call_args.args[2]
    await handler(
        SimpleNamespace(data={"access_token": "tok", "access_token_date_updated": "2026-11-07T22:00:00"})
    )
    first.async_refresh.assert_awaited_once()
    second.async_refresh.assert_awaited_once()
    saved = [c.kwargs["options"] for c in config_entries.async_update_entry.call_args_list]
    assert saved and all(
        o["access_token"] == "tok"
        and o["access_token_date_updated"] == "2026-11-07T22:00:00"
        and o["use_grouped_sensors"] is False
        for o in saved
    )
