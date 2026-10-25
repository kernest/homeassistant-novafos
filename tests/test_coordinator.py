"""Statistics import tests for the Novafos coordinator.

The recorder is replaced by a small in-memory store, except in the round
trip test at the end, so a refresh can be repeated and its result inspected
the way Home Assistant would see it.  No KMD or Home Assistant instance is
contacted and no real token is used.
"""

import asyncio
import copy
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

from custom_components.novafos import async_migrate_entry, update_listener
from custom_components.novafos import coordinator as coordinator_module
from custom_components.novafos import legacy_statistics
from custom_components.novafos.config_flow import OptionsFlowHandler
from custom_components.novafos.const import DOMAIN, HISTORY_RECONCILIATION_KEY
from custom_components.novafos.coordinator import NovafosUpdateCoordinator
from custom_components.novafos.pynovafos.novafos import HTTPFailed, Novafos

TZ = "Europe/Copenhagen"
NOW = "2026-10-08T12:00:00"
# STATISTICS_LOOKBACK_DAYS before NOW, at local midnight.
WINDOW_START = datetime(2026, 9, 7, tzinfo=ZoneInfo(TZ))
METERS = {
    "water": {"type": "water", "Unit": {"Name": "m³"}},
    "heating": {"type": "heating", "Unit": {"Name": "kWh"}},
}


async def run_executor(function, *args):
    return function(*args)


class FakeRecorder:
    """Statistics store with the recorder functions the coordinator uses."""

    def __init__(self):
        self.rows = {}
        self.metadata = {}
        self.imports = []
        self.period_queries = []

    def seed(self, statistic_id, start, state, total):
        timestamp = start.timestamp()
        self.rows.setdefault(statistic_id, {})[timestamp] = {
            "start": timestamp,
            "end": timestamp + 3600,
            "state": state,
            "sum": total,
        }

    def get_last_statistics(self, hass, count, statistic_id, convert_units, types):
        rows = self.rows.get(statistic_id)
        if not rows:
            return {}
        return {statistic_id: [dict(rows[max(rows)])]}

    def statistics_during_period(self, hass, start, end, ids, period, units, types):
        self.period_queries.append((start, end, set(ids)))
        result = {}
        for statistic_id in ids:
            matched = [
                dict(row)
                for timestamp, row in sorted(self.rows.get(statistic_id, {}).items())
                if timestamp >= start.timestamp()
                and (end is None or timestamp < end.timestamp())
            ]
            if matched:
                result[statistic_id] = matched
        return result

    def import_statistics(self, hass, metadata, statistics):
        statistic_id = metadata["statistic_id"]
        self.metadata[statistic_id] = metadata
        self.imports.append((statistic_id, list(statistics)))
        store = self.rows.setdefault(statistic_id, {})
        for point in statistics:
            timestamp = point["start"].timestamp()
            store[timestamp] = {
                "start": timestamp,
                "end": timestamp + 3600,
                "state": point.get("state"),
                "sum": point.get("sum"),
            }

    def imported(self, statistic_id):
        return [rows for imported_id, rows in self.imports if imported_id == statistic_id]

    def backfill_queries(self):
        return [query for query in self.period_queries if query[0].year == 1970]


@pytest.fixture
def fake_recorder(monkeypatch):
    fake = FakeRecorder()
    monkeypatch.setattr(
        coordinator_module,
        "get_instance",
        Mock(return_value=SimpleNamespace(async_add_executor_job=run_executor)),
    )
    monkeypatch.setattr(
        coordinator_module, "get_last_statistics", fake.get_last_statistics
    )
    monkeypatch.setattr(
        coordinator_module, "statistics_during_period", fake.statistics_during_period
    )
    monkeypatch.setattr(
        coordinator_module, "async_import_statistics", fake.import_statistics
    )
    monkeypatch.setattr(
        coordinator_module, "async_add_external_statistics", fake.import_statistics
    )
    monkeypatch.setattr(
        legacy_statistics, "async_import_statistics", fake.import_statistics
    )
    return fake


def hours(day, values):
    return [
        {"DateFrom": f"{day}T{hour:02d}:00:00", "Value": value}
        for hour, value in enumerate(values)
    ]


def seed_existing(fake, meter_type, total, state=0.1):
    """An installation that already imported history up to the window."""
    before = WINDOW_START - timedelta(hours=1)
    fake.seed(f"sensor.{DOMAIN}_{meter_type}_statistics", before, state, total)
    fake.seed(f"{DOMAIN}:{meter_type}_consumption", before, state, total)


def make_coordinator(meter_types, data, entry_data=None, entry_options=None):
    api = Mock()
    api.get_meter_types.return_value = [METERS[meter_type] for meter_type in meter_types]
    api.get_statistics.side_effect = lambda from_date: data
    api.get_available_time_series_periods.return_value = datetime(2026, 10, 5)
    api._meter_data = data
    coordinator = object.__new__(NovafosUpdateCoordinator)
    coordinator.hass = SimpleNamespace(
        async_add_executor_job=run_executor,
        config=SimpleNamespace(time_zone=TZ, currency="DKK"),
        config_entries=SimpleNamespace(async_update_entry=Mock()),
    )
    coordinator.api = api
    coordinator.entry = SimpleNamespace(
        data={"name": "Novafos", "use_grouped_sensors": False, **(entry_data or {})},
        options=entry_options or {},
    )
    coordinator.cumulative_totals = {}
    coordinator._auto_reconciled = False
    coordinator._cost_reconciled = False
    coordinator._schedule_full_history = Mock()
    return coordinator


# --- Refresh, token and options ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("data", "options", "legacy", "external"),
    [
        ({"use_grouped_sensors": True}, {}, True, True),
        (
            {"use_grouped_sensors": True},
            {"use_external_statistics": False},
            True,
            False,
        ),
        (
            {"use_grouped_sensors": False},
            {"use_external_statistics": True},
            False,
            True,
        ),
        ({"use_grouped_sensors": True}, {"use_grouped_sensors": False}, False, True),
        (
            {"use_grouped_sensors": False, "use_external_statistics": False},
            {},
            False,
            False,
        ),
    ],
)
async def test_statistics_options_are_independent(
    data, options, legacy, external, monkeypatch
):
    api = Mock()
    api.authenticate_using_access_token.return_value = True
    api.get_year_data.return_value = {}
    api._meter_data = {}
    import_grouped = AsyncMock()
    monkeypatch.setattr(
        coordinator_module, "async_import_grouped_statistics", import_grouped
    )
    entry = SimpleNamespace(data=data, options=options)
    coordinator = SimpleNamespace(
        hass=SimpleNamespace(async_add_executor_job=run_executor),
        api=api,
        entry=entry,
        access_token="",
        access_token_date_updated="",
        _import_lock=asyncio.Lock(),
        _insert_statistics=AsyncMock(),
    )

    await NovafosUpdateCoordinator._async_update_data(coordinator)

    coordinator._insert_statistics.assert_awaited_once()
    assert import_grouped.await_count == int(legacy)
    assert coordinator_module.use_external_statistics(entry) is external


@pytest.mark.asyncio
async def test_expired_token_fails_explicitly_without_writing(monkeypatch):
    """An expired token must not look like a successful zero-consumption refresh."""
    api = Mock()
    api.authenticate_using_access_token.return_value = False
    import_grouped = AsyncMock()
    monkeypatch.setattr(
        coordinator_module, "async_import_grouped_statistics", import_grouped
    )
    coordinator = SimpleNamespace(
        hass=SimpleNamespace(async_add_executor_job=run_executor),
        api=api,
        entry=SimpleNamespace(data={"use_grouped_sensors": True}, options={}),
        access_token="expired",
        access_token_date_updated="2020-01-01T00:00:00",
        _import_lock=asyncio.Lock(),
        _insert_statistics=AsyncMock(),
    )

    with pytest.raises(UpdateFailed, match="expired or invalid"):
        await NovafosUpdateCoordinator._async_update_data(coordinator)

    coordinator._insert_statistics.assert_not_awaited()
    import_grouped.assert_not_awaited()
    api.get_year_data.assert_not_called()
    api.get_dummy_data.assert_not_called()


def test_old_token_is_rejected_before_any_request(mocker):
    get = mocker.patch("requests.get")
    post = mocker.patch("requests.post")
    api = Novafos(timezone=TZ)

    assert not api.authenticate_using_access_token("x" * 1200, "2020-01-01T00:00:00")

    get.assert_not_called()
    post.assert_not_called()


@pytest.mark.asyncio
async def test_kmd_failure_during_import_raises_update_failed(monkeypatch):
    api = Mock()
    api.authenticate_using_access_token.return_value = True
    api.get_year_data.return_value = {}
    coordinator = SimpleNamespace(
        hass=SimpleNamespace(async_add_executor_job=run_executor),
        api=api,
        entry=SimpleNamespace(data={"use_grouped_sensors": False}, options={}),
        access_token="",
        access_token_date_updated="",
        _import_lock=asyncio.Lock(),
        _insert_statistics=AsyncMock(side_effect=HTTPFailed("timeout")),
    )

    with pytest.raises(UpdateFailed, match="unavailable"):
        await NovafosUpdateCoordinator._async_update_data(coordinator)


@pytest.mark.asyncio
async def test_options_keep_token_and_prices_when_changing_statistics_options(
    freezer,
):
    freezer.move_to(NOW)
    entry = SimpleNamespace(
        data={"use_grouped_sensors": True},
        options={
            "access_token": "saved-token",
            "access_token_date_updated": "2026-09-01T00:00:00",
            "water_prices": {"2025": 40.0},
        },
    )
    flow = OptionsFlowHandler(entry)
    flow.async_show_form = Mock(side_effect=lambda **kwargs: kwargs)
    flow.async_create_entry = Mock(side_effect=lambda **kwargs: kwargs)

    form = await flow.async_step_init()
    defaults = form["data_schema"]({})
    assert defaults["access_token"] == ""
    assert defaults["water_price"] == 40.0
    assert defaults["use_grouped_sensors"] is True
    assert defaults["use_external_statistics"] is True

    result = await flow.async_step_init({**defaults, "use_grouped_sensors": False})
    options = result["data"]
    assert options["access_token"] == "saved-token"
    assert options["access_token_date_updated"] == "2026-09-01T00:00:00"
    assert options["use_grouped_sensors"] is False
    assert options["use_external_statistics"] is True
    # Earlier years keep their own price.
    assert options["water_prices"] == {"2025": 40.0, "2026": 40.0}
    assert options["heating_prices"] == {}


@pytest.mark.asyncio
async def test_legacy_option_reloads_entities_external_option_refreshes():
    coordinator = SimpleNamespace(
        use_grouped_sensors=True,
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
    hass.config_entries.async_reload.assert_awaited_once_with("entry-id")


@pytest.mark.asyncio
async def test_migration_from_v4_requests_reconciliation_and_keeps_settings():
    entry = SimpleNamespace(
        version=4,
        data={"name": "Novafos", "use_grouped_sensors": True},
        options={"access_token": "token", "access_token_date_updated": ""},
    )

    def update_entry(config_entry, **changes):
        for key in ("data", "options", "version"):
            if key in changes:
                setattr(config_entry, key, changes[key])

    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_update_entry=Mock(side_effect=update_entry))
    )

    assert await async_migrate_entry(hass, entry)

    assert entry.version == 5
    assert entry.data == {
        "name": "Novafos",
        "use_grouped_sensors": True,
        HISTORY_RECONCILIATION_KEY: True,
    }
    assert entry.options == {"access_token": "token", "access_token_date_updated": ""}
    # Options added after version 5 fall back to safe defaults.
    assert coordinator_module.use_grouped_sensors(entry) is True
    assert coordinator_module.use_external_statistics(entry) is True


# --- Statistics import ------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("meter_types", [("water", "heating"), ("heating", "water")])
async def test_combined_water_and_heating_statistics_stay_separate(
    fake_recorder, freezer, meter_types
):
    freezer.move_to(NOW)
    seed_existing(fake_recorder, "water", 100.0)
    seed_existing(fake_recorder, "heating", 5000.0)
    data = {
        "water": hours("2026-10-06", [0.5, 0.25]),
        "heating": hours("2026-10-06", [20.0, 10.0]),
    }
    coordinator = make_coordinator(meter_types, data)

    await NovafosUpdateCoordinator._insert_statistics(coordinator, None, debug=False)

    # One KMD fetch for both meters, for the rolling window.
    coordinator.api.get_statistics.assert_called_once_with(datetime(2026, 9, 7))
    coordinator._schedule_full_history.assert_not_called()
    for meter_type, base, values, unit, unit_class in (
        ("water", 100.0, [0.5, 0.25], "m³", "volume"),
        ("heating", 5000.0, [20.0, 10.0], "kWh", "energy"),
    ):
        sums = [base + values[0], base + values[0] + values[1]]
        for statistic_id in (
            f"sensor.{DOMAIN}_{meter_type}_statistics",
            f"{DOMAIN}:{meter_type}_consumption",
        ):
            [rows] = fake_recorder.imported(statistic_id)
            assert [row["state"] for row in rows] == values
            assert [row["sum"] for row in rows] == pytest.approx(sums)
            # Total statistics carry no mean, min or max.
            assert all(not {"min", "max", "mean"} & set(row) for row in rows)
            metadata = fake_recorder.metadata[statistic_id]
            assert metadata["mean_type"] is StatisticMeanType.NONE
            assert metadata["has_sum"] is True
            assert metadata["unit_of_measurement"] == unit
            assert metadata["unit_class"] == unit_class

        hourly_id = f"{DOMAIN}:{meter_type}_hourly_consumption"
        [hourly] = fake_recorder.imported(hourly_id)
        # The hour before the window is backfilled from the consumption
        # statistic, then the window follows with the same sums.
        assert [row["mean"] for row in hourly] == [0.1] + values
        assert [row["min"] for row in hourly] == [0.1] + values
        assert [row["max"] for row in hourly] == [0.1] + values
        assert [row["sum"] for row in hourly] == pytest.approx([base] + sums)
        metadata = fake_recorder.metadata[hourly_id]
        assert valid_statistic_id(hourly_id)
        assert metadata["source"] == DOMAIN
        assert metadata["mean_type"] is StatisticMeanType.ARITHMETIC
        assert metadata["unit_of_measurement"] == unit
        assert coordinator.cumulative_totals[meter_type] == pytest.approx(sums[-1])


@pytest.mark.asyncio
async def test_rolling_refresh_is_deterministic(fake_recorder, freezer):
    """Re-importing the same window must not duplicate or shift any sum."""
    freezer.move_to(NOW)
    seed_existing(fake_recorder, "water", 100.0)
    seed_existing(fake_recorder, "heating", 5000.0)
    data = {
        "water": hours("2026-10-06", [0.5, 0.25, 0.0]),
        "heating": hours("2026-10-06", [20.0, 10.0, 5.0]),
    }
    coordinator = make_coordinator(("water", "heating"), data)

    await NovafosUpdateCoordinator._insert_statistics(coordinator, None, debug=False)
    after_first = copy.deepcopy(fake_recorder.rows)
    await NovafosUpdateCoordinator._insert_statistics(coordinator, None, debug=False)

    assert fake_recorder.rows == after_first
    water = fake_recorder.rows[f"sensor.{DOMAIN}_water_statistics"]
    assert water[max(water)]["sum"] == pytest.approx(100.75)
    # Older hours are copied into the hourly statistic only once per meter.
    assert len(fake_recorder.backfill_queries()) == 2
    coordinator._schedule_full_history.assert_not_called()


@pytest.mark.asyncio
async def test_delayed_meter_is_skipped_without_touching_its_statistics(
    fake_recorder, freezer
):
    """KMD publishing water late must neither fail nor zero-fill water."""
    freezer.move_to(NOW)
    seed_existing(fake_recorder, "water", 100.0)
    seed_existing(fake_recorder, "heating", 5000.0)
    data = {"water": [], "heating": hours("2026-10-06", [20.0])}
    coordinator = make_coordinator(("water", "heating"), data)

    await NovafosUpdateCoordinator._insert_statistics(coordinator, None, debug=False)

    for statistic_id in (
        f"sensor.{DOMAIN}_water_statistics",
        f"{DOMAIN}:water_consumption",
        f"{DOMAIN}:water_hourly_consumption",
    ):
        assert fake_recorder.imported(statistic_id) == []
    assert "water" not in coordinator.cumulative_totals
    [heating] = fake_recorder.imported(f"sensor.{DOMAIN}_heating_statistics")
    assert [row["sum"] for row in heating] == [5020.0]


@pytest.mark.asyncio
async def test_no_data_at_all_imports_nothing(fake_recorder, freezer):
    freezer.move_to(NOW)
    seed_existing(fake_recorder, "water", 100.0)
    coordinator = make_coordinator(("water",), {})

    await NovafosUpdateCoordinator._insert_statistics(coordinator, None, debug=False)

    assert fake_recorder.imports == []
    assert coordinator.cumulative_totals == {}


@pytest.mark.asyncio
async def test_new_installation_schedules_full_history_in_background(
    fake_recorder, freezer
):
    freezer.move_to(NOW)
    data = {"water": hours("2026-10-06", [0.5, 0.25])}
    coordinator = make_coordinator(("water",), data)

    await NovafosUpdateCoordinator._insert_statistics(coordinator, None, debug=False)

    coordinator._schedule_full_history.assert_called_once_with(None)
    coordinator.api.get_available_time_series_periods.assert_not_called()
    [rows] = fake_recorder.imported(f"sensor.{DOMAIN}_water_statistics")
    assert [row["sum"] for row in rows] == [0.5, 0.75]
    [hourly] = fake_recorder.imported(f"{DOMAIN}:water_hourly_consumption")
    assert [row["sum"] for row in hourly] == [0.5, 0.75]


@pytest.mark.asyncio
async def test_full_history_import_rebuilds_from_zero_and_clears_migration(
    fake_recorder, freezer
):
    freezer.move_to(NOW)
    # Stale sums from an older version must not leak into the rebuild.
    seed_existing(fake_recorder, "water", 999.0)
    seed_existing(fake_recorder, "heating", 999.0)
    data = {
        "water": hours("2026-10-05", [0.5, 0.25]),
        "heating": hours("2026-10-05", [20.0]),
    }
    coordinator = make_coordinator(
        ("water", "heating"), data, entry_data={HISTORY_RECONCILIATION_KEY: True}
    )

    await NovafosUpdateCoordinator._insert_statistics(
        coordinator, None, debug=False, full_history=True
    )

    coordinator.api.get_statistics.assert_called_once_with(datetime(2026, 10, 5))
    coordinator._schedule_full_history.assert_not_called()
    for meter_type, sums in (("water", [0.5, 0.75]), ("heating", [20.0])):
        for statistic_id in (
            f"sensor.{DOMAIN}_{meter_type}_statistics",
            f"{DOMAIN}:{meter_type}_consumption",
            f"{DOMAIN}:{meter_type}_hourly_consumption",
        ):
            [rows] = fake_recorder.imported(statistic_id)
            assert [row["sum"] for row in rows] == pytest.approx(sums)
    # The full import writes the whole hourly series itself.
    assert fake_recorder.backfill_queries() == []
    update = coordinator.hass.config_entries.async_update_entry
    update.assert_called_once()
    assert HISTORY_RECONCILIATION_KEY not in update.call_args.kwargs["data"]


@pytest.mark.asyncio
async def test_external_hourly_statistics_can_be_disabled(fake_recorder, freezer):
    freezer.move_to(NOW)
    seed_existing(fake_recorder, "water", 100.0)
    data = {"water": hours("2026-10-06", [0.5])}
    coordinator = make_coordinator(
        ("water",), data, entry_options={"use_external_statistics": False}
    )

    await NovafosUpdateCoordinator._insert_statistics(coordinator, None, debug=False)

    assert fake_recorder.imported(f"{DOMAIN}:water_hourly_consumption") == []
    assert fake_recorder.backfill_queries() == []
    # The Energy dashboard statistic does not depend on the option.
    [rows] = fake_recorder.imported(f"{DOMAIN}:water_consumption")
    assert [row["sum"] for row in rows] == [100.5]


@pytest.mark.asyncio
async def test_grouped_statistics_have_no_mean_and_follow_kmd_unit(fake_recorder):
    api = Mock()
    api.get_meter_types.return_value = [
        METERS["water"],
        {"type": "heating", "Unit": {"Name": "MWh"}},
    ]
    api.get_grouped_statistics.side_effect = lambda meter_type, grouping: (
        []
        if grouping == "week"
        else [
            (
                "2026-09-26T00:00:00",
                10.0 if meter_type == "water" else 0.03,
                0.5,
                0.0,
                0.5,
                0.25,
            )
        ]
    )
    hass = SimpleNamespace(config=SimpleNamespace(time_zone=TZ))

    await legacy_statistics.async_import_grouped_statistics(
        hass, api, grouping=("day", "week")
    )

    # No complete week yet: nothing is written.
    assert fake_recorder.imported(f"sensor.{DOMAIN}_water_statistics_week") == []
    [rows] = fake_recorder.imported(f"sensor.{DOMAIN}_heating_statistics_day")
    assert rows == [
        {"start": datetime(2026, 9, 26, tzinfo=ZoneInfo(TZ)), "state": 0.03, "sum": 0.03}
    ]
    metadata = fake_recorder.metadata[f"sensor.{DOMAIN}_heating_statistics_day"]
    assert metadata["mean_type"] is StatisticMeanType.NONE
    assert metadata["unit_of_measurement"] == "MWh"
    assert metadata["unit_class"] == "energy"
    water = fake_recorder.metadata[f"sensor.{DOMAIN}_water_statistics_day"]
    assert water["unit_class"] == "volume"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("day", "hour_count"),
    [(date(2026, 3, 29), 23), (date(2026, 10, 25), 25)],
)
async def test_hourly_statistics_backfill_handles_dst(
    monkeypatch, fake_recorder, day, hour_count
):
    timezone_local = ZoneInfo(TZ)
    day_start = datetime.combine(day, datetime.min.time(), timezone_local)
    next_day_start = datetime.combine(
        day + timedelta(days=1), datetime.min.time(), timezone_local
    )
    start_utc = day_start.astimezone(timezone.utc)
    next_start_utc = next_day_start.astimezone(timezone.utc)
    assert (next_start_utc - start_utc) == timedelta(hours=hour_count)
    for hour in range(hour_count):
        fake_recorder.seed(
            f"{DOMAIN}:water_consumption",
            start_utc + timedelta(hours=hour),
            0.1,
            round((hour + 1) * 0.1, 3),
        )
    # Let Home Assistant validate the statistic id, metadata and timestamps.
    recorder = Mock()
    monkeypatch.setattr(
        recorder_statistics, "get_instance", Mock(return_value=recorder)
    )
    monkeypatch.setattr(
        coordinator_module,
        "async_add_external_statistics",
        recorder_statistics.async_add_external_statistics,
    )
    coordinator = SimpleNamespace(
        hass=SimpleNamespace(config=SimpleNamespace(time_zone=TZ))
    )

    await NovafosUpdateCoordinator._insert_external_hourly_statistics(
        coordinator, "water", "m³", "volume", [], next_day_start, False
    )

    recorder.async_import_statistics.assert_called_once()
    metadata, rows = recorder.async_import_statistics.call_args.args[:2]
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
    assert len(fake_recorder.backfill_queries()) == 1


@pytest.mark.asyncio
async def test_external_hourly_statistics_recorder_roundtrip(
    async_setup_recorder_instance, hass
):
    recorder = await async_setup_recorder_instance(hass)
    start = datetime(2026, 9, 21, tzinfo=ZoneInfo(hass.config.time_zone)).astimezone(
        timezone.utc
    )
    statistic_id = "novafos:water_hourly_consumption"
    recorder_statistics.async_add_external_statistics(
        hass,
        StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name="Novafos water consumption",
            source="novafos",
            statistic_id="novafos:water_consumption",
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
    coordinator = SimpleNamespace(hass=hass)

    # Enabling the option on an existing installation copies older hours.
    await NovafosUpdateCoordinator._insert_external_hourly_statistics(
        coordinator, "water", "m³", "volume", [], start + timedelta(hours=48), False
    )
    await hass.async_add_executor_job(recorder.block_till_done)

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
    assert [row["mean"] for row in daily[statistic_id]] == [1.0, 2.0]
    assert [row["change"] for row in daily[statistic_id]] == [24.0, 48.0]

    # A later refresh replaces a corrected hour inside its window directly.
    await NovafosUpdateCoordinator._insert_external_hourly_statistics(
        coordinator,
        "water",
        "m³",
        "volume",
        [StatisticData(start=start + timedelta(hours=47), state=2.5, sum=72.5)],
        start + timedelta(hours=47),
        False,
    )
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
