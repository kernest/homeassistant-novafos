"""DataUpdateCoordinator for Novafos."""

from __future__ import annotations

from .pynovafos.novafos import Novafos

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.exceptions import HomeAssistantError

import asyncio
from datetime import datetime as dt
from datetime import timedelta


from .const import (
    DOMAIN,
    HISTORY_RECONCILIATION_KEY,
    RECONCILIATION_TOLERANCE_ABS,
    RECONCILIATION_TOLERANCE_REL,
    STATISTICS_LOOKBACK_DAYS,
    meter_unit,
    price_option,
    prices_option,
)
from homeassistant.components.recorder import DOMAIN as RECORDER_DOMAIN, get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMetaData,
    StatisticMeanType,
)
from homeassistant.components.recorder.statistics import (
    get_last_statistics,
    statistics_during_period,
    async_import_statistics,
    async_add_external_statistics,
)
from homeassistant.util import dt as dt_util
from typing import cast

# If debugging, use pre-seeded data:
# from .pynovafos.sample_data import (
#     get_active_meters,
#     get_year_sample_data,
#     get_year_sample_data_extra,
# )

import logging

_LOGGER = logging.getLogger(__name__)


class NovafosUpdateCoordinator(DataUpdateCoordinator):
    """DataUpdateCoordinator for Novafos."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: Novafos,
        entry: ConfigEntry,
    ) -> None:
        """Initialize DataUpdateCoordinator"""
        self.api = api
        self.hass = hass
        self.entry = entry
        # Attribute data for the sensors
        self.attrib_data = {"year_total": None}
        # Latest cumulative sums imported into recorder.  The corresponding
        # sensor entities expose these values so Home Assistant's Energy
        # dashboard can validate and select them.
        self.cumulative_totals: dict[str, float] = {}
        # Set once an automatic discrepancy reconciliation has run, so a
        # persistent mismatch cannot trigger a full import on every refresh.
        self._auto_reconciled = False
        # Same for the one full import that creates the cost history.
        self._cost_reconciled = False
        # The full history import makes one KMD request per day and meter and
        # can take minutes, longer than Home Assistant allows setup to block.
        # It runs as a background task; the lock keeps it and regular
        # refreshes from sharing the API client's data at the same time.
        self._full_history_task: asyncio.Task | None = None
        self._import_lock = asyncio.Lock()
        # Need local version here to enable updating via action service calls
        self.access_token = (
            self.entry.options["access_token"]
            if "access_token" in self.entry.options
            else ""
        )
        self.access_token_date_updated = (
            self.entry.options["access_token_date_updated"]
            if "access_token_date_updated" in self.entry.options
            else ""
        )

        super().__init__(hass, _LOGGER, name="Novafos")

    async def _async_update_data(self):
        """Get the data for Novafos."""
        _LOGGER.debug("Performing token based authentication")

        debug = False
        # If debugging and need to re-seed the database:
        # if debug:
        #     # Pre-seed data from file
        #     self.api._active_meters = get_active_meters()
        #     self.api._meter_data = get_year_sample_data()
        #     self.api._meter_data_extra = get_year_sample_data_extra()

        meter_year_data = None
        if await self.hass.async_add_executor_job(
            self.api.authenticate_using_access_token,
            self.access_token,
            self.access_token_date_updated,
        ):
            # Retrieve latest data from the API
            # if True:
            try:
                _LOGGER.debug("Getting latest statistics")
                meter_year_data = await self.hass.async_add_executor_job(
                    self.api.get_year_data
                )
                # last_state = await self._insert_statistics(debug=debug)
                async with self._import_lock:
                    await self._insert_statistics(meter_year_data, debug=debug)
                    if self.entry.data["use_grouped_sensors"]:
                        await self._insert_grouped_statistics(debug=debug)
                    data = (self.api._meter_data, meter_year_data)  # , last_state)
            except Exception as ex:
                _LOGGER.exception("Error while updating Novafos data")
                raise UpdateFailed(f"The service is unavailable: {ex}") from ex
        else:
            # Do not publish dummy readings as a successful refresh. This made
            # expired tokens look like valid zero consumption and prevented the
            # user from seeing that KMD was never queried.
            raise UpdateFailed(
                "The KMD access token is expired or invalid. Update it in the "
                "NovaFos integration options."
            )

        # The data is stored in the coordinator as a .data field.
        _LOGGER.debug("Returning from Coordinator with data: %s", data)
        return data

    def _schedule_full_history(self, meter_year_data) -> None:
        """Start the full history import in the background, once at a time."""
        if self._full_history_task and not self._full_history_task.done():
            return
        self._full_history_task = self.entry.async_create_background_task(
            self.hass,
            self._run_full_history(meter_year_data),
            "novafos_full_history_reconciliation",
        )

    async def _run_full_history(self, meter_year_data) -> None:
        """Import all available KMD history and publish the result."""
        try:
            async with self._import_lock:
                await self._insert_statistics(
                    meter_year_data, debug=False, full_history=True
                )
                if self.entry.data["use_grouped_sensors"]:
                    await self._insert_grouped_statistics(debug=False)
                data = (self.api._meter_data, meter_year_data)
            self.async_set_updated_data(data)
            _LOGGER.info("Full KMD history reconciliation finished")
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception(
                "Full KMD history reconciliation failed; it is retried on the next refresh"
            )
            self._auto_reconciled = False

    async def _insert_statistics(
        self, meter_year_data, debug, full_history=False
    ) -> None:
        """Update statistics when data is returned.

        A regular refresh imports the rolling window and, when a full import
        is needed, schedules it in the background.  full_history=True is that
        background import.
        """
        meter_devices = self.api.get_meter_types()
        migration_requested = bool(
            self.entry.data.get(HISTORY_RECONCILIATION_KEY, False)
        )

        last_statistics = {}
        for meter_device in meter_devices:
            meter_type = meter_device["type"]
            statistic_id = f"sensor.{DOMAIN}_{meter_type}_statistics"
            last_statistics[meter_type] = await get_instance(
                self.hass
            ).async_add_executor_job(
                get_last_statistics, self.hass, 1, statistic_id, True, set()
            )

        # An old integration could leave recent recorder-generated rows while
        # most KMD history was never imported. A version migration explicitly
        # requests a complete rebuild. A new installation with no statistics
        # also starts at KMD's earliest available timestamp.
        if not full_history and (
            migration_requested
            or any(not last_statistics[meter["type"]] for meter in meter_devices)
        ):
            _LOGGER.info("Scheduling a full KMD history import in the background")
            self._schedule_full_history(meter_year_data)

        if full_history:
            min_date = await self.hass.async_add_executor_job(
                self.api.get_available_time_series_periods
            )
            fetch_start = min_date or (dt.now() - timedelta(days=365))
            fetch_start = fetch_start.replace(hour=0, minute=0, second=0, microsecond=0)
            _LOGGER.info("Reconciling all available KMD history since %s", fetch_start)
        else:
            fetch_start = (dt.now() - timedelta(days=STATISTICS_LOOKBACK_DAYS)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            _LOGGER.debug(
                "Refreshing the last %s days of KMD history since %s",
                STATISTICS_LOOKBACK_DAYS,
                fetch_start,
            )

        if debug:
            data = self.api._meter_data
        else:
            # get_statistics retrieves all active meters, so call it only once.
            data = await self.hass.async_add_executor_job(
                self.api.get_statistics, fetch_start
            )

        year_start = dt_util.now().replace(
            month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
        mismatched_meters = []
        coverage = {}

        # Iterate over water/heating
        # _get_meter_types returns:
        #      [{'type': 'water', 'InstallationId': 12345678, 'MeasurementPointId': 23456789, 'Unit': {'Id': 11111, 'Name': 'm³', 'Description': 'Vand', 'Decimals': 0, 'Order': 1}}]
        for meter_device in meter_devices:
            meter_type = meter_device["type"]
            _LOGGER.debug("Retrieving statistics data for %s meter.", meter_type)
            # Cumulative sum just before 1 January, when it falls inside the
            # imported window.  Otherwise it is read from recorder below.
            sum_before_year = None

            statistic_id = f"sensor.{DOMAIN}_{meter_type}_statistics"
            unit, unit_class, _ = meter_unit(meter_device)

            _LOGGER.debug("Last statistics (raw): %s", last_statistics[meter_type])
            if full_history:
                _sum = 0.0
            else:
                # Anchor the rolling replacement window to the last cumulative
                # sum immediately before it. Selecting the first row from an
                # open-ended query used an old sum and corrupted later imports.
                # fetch_start is naive local time; recorder needs it aware.
                anchor_sum = await self._sum_before(
                    statistic_id,
                    fetch_start.replace(
                        tzinfo=dt_util.get_time_zone(self.hass.config.time_zone)
                    ),
                )
                _LOGGER.debug("Cumulative anchor before %s: %s", fetch_start, anchor_sum)
                if anchor_sum is not None:
                    _sum = anchor_sum
                else:
                    _LOGGER.warning(
                        "No cumulative anchor found before %s for %s; rebuilding this window from zero",
                        fetch_start,
                        meter_type,
                    )
                    _sum = 0.0

            if not data.get(meter_type):
                # KMD publishes hourly readings with a delay, so a refresh can
                # legitimately return no new points.  Skip instead of failing.
                _LOGGER.info(
                    "No new hourly data returned for %s meter - skipping statistics update",
                    meter_type,
                )
                continue

            # Array of statistics points
            statistics = []

            # Populate statistics array
            last_value = data[meter_type][0]["Value"]
            for val in data[meter_type]:
                # Add timezone to dataset as Home Assistant works in UTC
                from_time = dt_util.parse_datetime(f"{val['DateFrom']}").replace(
                    tzinfo=dt_util.get_time_zone(self.hass.config.time_zone)
                )
                if sum_before_year is None and from_time >= year_start:
                    sum_before_year = _sum
                _sum += val["Value"]
                _max = last_value if val["Value"] < last_value else val["Value"]
                _min = last_value if val["Value"] >= last_value else val["Value"]
                _mean = (_min + _max) / 2
                last_value = val["Value"]
                # _LOGGER.debug(f"Adding: {from_time}, {val["Value"]}, {_sum}")

                statistics.append(
                    StatisticData(
                        start=from_time,
                        state=val["Value"],
                        sum=_sum,
                        min=_min,
                        max=_max,
                        mean=_mean,
                    )
                )

            # For min/max/average check out https://github.com/emontnemery/home-assistant/blob/dev/homeassistant/components/kitchen_sink/__init__.py#L148,
            # https://github.com/emontnemery/home-assistant/blob/dev/homeassistant/components/recorder/models/statistics.py#L31
            # metadata = StatisticMetaData(
            #     mean_type=StatisticMeanType.ARITHMETIC,
            #     has_sum=True,
            #     name=f"Novafos {meter_type} Statistics",
            #     source=DOMAIN,
            #     statistic_id=statistic_id,
            #     unit_of_measurement=unit,
            # )
            # Creates a new statistics "novafos:<name>".  This is different from the sensor.<name>
            # async_add_external_statistics(self.hass, metadata, statistics)

            # Apexchart does not understand the domain:statistic noation, so we'll use an internal sensor instead.
            # Update the sensor statistics.  Only the hourly one is needed as the sensor can then aggregate data itself.
            metadata = StatisticMetaData(
                # These entities have state_class total, for which Home
                # Assistant expects no mean.  An arithmetic mean here made
                # recorder report "The mean type has changed".
                mean_type=StatisticMeanType.NONE,
                has_sum=True,
                name=None,
                source=RECORDER_DOMAIN,
                statistic_id=statistic_id,
                unit_class=unit_class,
                unit_of_measurement=unit,
            )
            async_import_statistics(self.hass, metadata, statistics)
            # Keep the entity state aligned with the manually imported
            # recorder sum.  Publishing the raw hourly value here would make
            # recorder calculate incorrect long-term statistics, while a
            # cumulative value is safe and makes the entity eligible for the
            # Energy dashboard.
            self.cumulative_totals[meter_type] = _sum
            await self._import_cost_statistics(
                meter_type, statistics, fetch_start, full_history, meter_year_data
            )

            points = data[meter_type]
            nonzero = [p["DateFrom"] for p in points if p["Value"]]
            nonzero_days = sorted({date[:10] for date in nonzero})
            coverage[meter_type] = (
                f"{len(points)} hourly points {points[0]['DateFrom']} to "
                f"{points[-1]['DateFrom']} summing "
                f"{sum(p['Value'] or 0 for p in points):.3f}, {len(nonzero)} non-zero"
                f" on {len(nonzero_days)} day(s)"
                + (f": {', '.join(nonzero_days[:12])}" if nonzero_days else "")
                + (" ..." if len(nonzero_days) > 12 else "")
            )

            kmd_year_total = self._kmd_year_total(meter_year_data, meter_type)
            if kmd_year_total is None:
                continue
            first_imported = dt_util.parse_datetime(
                f"{data[meter_type][0]['DateFrom']}"
            ).replace(tzinfo=dt_util.get_time_zone(self.hass.config.time_zone))
            if not full_history and first_imported > year_start:
                # A rolling window that starts after 1 January: the sum before
                # the year comes from rows recorder already holds.  A full
                # import restarts the sum at zero, so older recorder rows must
                # not be used; the in-memory value above is correct there.
                sum_before_year = await self._sum_before(statistic_id, year_start)
            ha_year_total = _sum - (sum_before_year or 0.0)
            tolerance = max(
                RECONCILIATION_TOLERANCE_ABS,
                abs(kmd_year_total) * RECONCILIATION_TOLERANCE_REL,
            )
            _LOGGER.debug(
                "Year-to-date %s: Home Assistant %.3f, KMD %.3f",
                meter_type,
                ha_year_total,
                kmd_year_total,
            )
            if abs(ha_year_total - kmd_year_total) > tolerance:
                mismatched_meters.append((meter_type, ha_year_total, kmd_year_total))

        if mismatched_meters:
            details = ", ".join(
                f"{meter_type}: Home Assistant {ha:.3f} vs KMD {kmd:.3f}"
                f" [{coverage[meter_type]}]"
                for meter_type, ha, kmd in mismatched_meters
            )
            if not full_history and not self._auto_reconciled:
                # Only this in-memory flag stops a repeat, so a persistent
                # mismatch costs at most one extra full import per HA start.
                self._auto_reconciled = True
                _LOGGER.warning(
                    "Year-to-date totals differ from KMD (%s); running a full history reconciliation in the background",
                    details,
                )
                self._schedule_full_history(meter_year_data)
            elif full_history:
                _LOGGER.warning(
                    "Year-to-date totals still differ from KMD after reconciliation (%s)",
                    details,
                )

        if full_history and migration_requested:
            # Persist completion only after every meter was processed without
            # raising. This makes the expensive all-history import one-time,
            # while failed attempts are retried safely on the next refresh.
            entry_data = dict(self.entry.data)
            entry_data.pop(HISTORY_RECONCILIATION_KEY, None)
            self.hass.config_entries.async_update_entry(self.entry, data=entry_data)

    def _prices(self, meter_type) -> dict[int, float]:
        """Return the configured price per year for a meter type."""
        raw = self.entry.options.get(prices_option(meter_type))
        if not raw:
            # A single price saved before prices were kept per year.
            legacy = self.entry.options.get(price_option(meter_type))
            raw = {str(dt.now().year): legacy} if legacy else {}
        prices = {}
        for year, price in raw.items():
            try:
                if float(price) > 0:
                    prices[int(year)] = float(price)
            except (TypeError, ValueError):
                continue
        return prices

    @staticmethod
    def _price_for_year(prices: dict[int, float], year: int) -> float:
        """Price of the latest year up to year, else the earliest price.

        History from before the first priced year uses the earliest price,
        so the price entered when cost tracking starts covers all history
        imported at that time.
        """
        earlier = [y for y in prices if y <= year]
        return prices[max(earlier)] if earlier else prices[min(prices)]

    async def _import_cost_statistics(
        self, meter_type, statistics, fetch_start, full_history, meter_year_data
    ) -> None:
        """Import the cumulative cost of the imported consumption.

        Each hour is priced with the price of its own year, and the cost sum
        continues from the stored cost before the window.  Changing the price
        therefore never reprices hours from earlier years.  The Energy
        dashboard can use this statistic under "Use an entity tracking the
        total costs", which, unlike a price entered there, covers history.
        """
        prices = self._prices(meter_type)
        if not prices or not statistics:
            return
        statistic_id = f"{DOMAIN}:{meter_type}_cost"
        if full_history:
            cost = 0.0
        else:
            cost = await self._sum_before(
                statistic_id,
                fetch_start.replace(
                    tzinfo=dt_util.get_time_zone(self.hass.config.time_zone)
                ),
            )
            if cost is None:
                # No stored cost yet: price the whole history once, in the
                # background, rather than starting the cost at zero here.
                if not self._cost_reconciled:
                    self._cost_reconciled = True
                    _LOGGER.info(
                        "No stored %s cost yet; importing all history in the background",
                        meter_type,
                    )
                    self._schedule_full_history(meter_year_data)
                return

        cost_statistics = []
        for point in statistics:
            cost += point["state"] * self._price_for_year(prices, point["start"].year)
            cost_statistics.append(
                StatisticData(start=point["start"], state=cost, sum=cost)
            )
        metadata = StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Novafos {meter_type} cost",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class=None,
            unit_of_measurement=self.hass.config.currency,
        )
        async_add_external_statistics(self.hass, metadata, cost_statistics)

    @staticmethod
    def _kmd_year_total(meter_year_data, meter_type) -> float | None:
        """Return KMD's total for the current year, or None if unavailable."""
        year_prefix = str(dt.now().year)
        rows = (meter_year_data or {}).get(meter_type, {}).get("Data", [])
        values = [
            row["Value"]
            for row in rows
            if row.get("Value") is not None
            and str(row.get("DateFrom", "")).startswith(year_prefix)
        ]
        return sum(values) if values else None

    async def _sum_before(self, statistic_id, point) -> float | None:
        """Return the recorder's cumulative sum in the hour before point."""
        stat = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            point - timedelta(days=1),
            point,
            {statistic_id},
            "hour",
            None,
            {"sum"},
        )
        rows = stat.get(statistic_id, [])
        return cast(float, rows[-1]["sum"]) if rows else None

    async def _insert_grouped_statistics(
        self, grouping=("day", "week", "month", "year"), debug=False
    ) -> None:
        """Update statistics when data is returned"""
        # Iterate over water/heating
        # _get_meter_types returns:
        #      [{'type': 'water', 'InstallationId': 11223344, 'MeasurementPointId': 33445566, 'Unit': {'Id': 10319, 'Name': 'm³', 'Description': 'Vand', 'Decimals': 0, 'Order': 1}}]
        for meter_device in self.api.get_meter_types():
            for period in grouping:
                meter_type = meter_device["type"]
                _LOGGER.debug(
                    "Generating grouped statistics data for %s meter for %s.",
                    meter_type,
                    period,
                )

                dataset = self.api.get_grouped_statistics(meter_type, period)
                statistic_id = f"sensor.{DOMAIN}_{meter_type}_statistics_{period}"
                unit, unit_class, _ = meter_unit(meter_device)

                # Naive version - just recalculate the complete history of the sensor data

                # Array of statistics points
                statistics = []

                # Populate statistics array
                for date, _sum, _change, _min, _max, _mean in dataset:
                    # Add timezone to dataset as Home Assistant works in UTC
                    from_time = dt_util.parse_datetime(date).replace(
                        tzinfo=dt_util.get_time_zone(self.hass.config.time_zone)
                    )
                    # _LOGGER.debug(f"Adding: {from_time}, {val["Value"]}, {_sum}")

                    statistics.append(
                        StatisticData(
                            start=from_time,
                            state=_sum,
                            sum=_sum,
                            min=_min,
                            max=_max,
                            mean=_mean,
                        )
                    )

                metadata = StatisticMetaData(
                    # state_class total: no mean, as for the hourly statistic.
                    mean_type=StatisticMeanType.NONE,
                    has_sum=True,
                    name=None,
                    source=RECORDER_DOMAIN,
                    statistic_id=statistic_id,
                    unit_class=unit_class,
                    unit_of_measurement=unit,
                )
                async_import_statistics(self.hass, metadata, statistics)


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""
