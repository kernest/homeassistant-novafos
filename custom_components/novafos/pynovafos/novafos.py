"""
Primary public module for novafos.dk API wrapper.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from zoneinfo import ZoneInfo
import logging
import json
import requests

_LOGGER = logging.getLogger(__name__)


class LoginFailed(Exception):
    """ "Exception class for bad credentials"""


class HTTPFailed(Exception):
    """Exception class for API HTTP failures"""


class Novafos:
    """
    Primary exported interface for KMD API wrapper.
    """

    DEFAULT_TIMEOUT = (10, 60)
    # KMD's hourly endpoint has returned readings only for the first day of a
    # multi-day range, with zeros for the rest and a range total that matches
    # those zeros.  One day per request is the only reliable choice.
    DEFAULT_CHUNK_DAYS = 1

    def __init__(
        self,
        timezone,
        customer_id=None,
        measurement_point_id=None,
        request_timeout=None,
        chunk_days=None,
    ):
        self._api_url = "https://easy-energy-plugin-api.kmd.dk"
        self.tz = ZoneInfo(timezone)
        self._request_timeout = request_timeout or self.DEFAULT_TIMEOUT
        self._chunk_days = chunk_days or self.DEFAULT_CHUNK_DAYS

        self._selected_customer_id = (
            str(customer_id) if customer_id is not None else None
        )
        self._selected_measurement_point_id = (
            str(measurement_point_id) if measurement_point_id is not None else None
        )
        self._access_token = ""
        self._customer_id = ""
        self._customer_number = ""
        self._active_meters = []
        self._earliest_data_date = None
        self._meter_data = {}
        self._meter_data_extra = {}
        self._last_valid_day = None
        # Raw availableTimeSeriesPeriods response; also lists units that
        # customerActiveMeters leaves out.  None until first fetched.
        self._time_series_periods = None

        # NOTE: Added because of reCAPCTHA login screen
        self._access_token_date_updated = ""

        # Zoom level is the granuarity of the retrieved data
        self._zoom_level = {"Year": 0, "Month": 1, "Day": 2, "Hour": 3, "Billing": 4}

    def _print_json(self, map, context="JSON dump"):
        _LOGGER.debug(
            "%s:\n %s",
            context,
            json.dumps(map, indent=4, sort_keys=True, ensure_ascii=False),
        )

    def authenticate_using_access_token(self, access_token, access_token_date_updated):
        """The only reason for this function is due to to a reCAPTCHA challenge on the Novafos login page.
        The date is used to verify the token is not too old when getting data.
        """
        self._access_token_date_updated = access_token_date_updated
        # Make a simple validation of the access_token length and only update if it has the correct length
        if len(access_token) >= 1200:
            self._access_token = "Bearer " + access_token
        else:
            self._access_token = ""
            _LOGGER.error(
                "Token update does not seem to have a valid length %s. Please check again. (This message is normal the first time the integration starts)",
                len(access_token),
            )
            return False
        # Never write the bearer token to Home Assistant's logs.
        _LOGGER.debug(
            "Access token accepted at date: '%s'",
            self._access_token_date_updated,
        )

        # Check token age
        now = datetime.now()
        # Allow 45 minutes since the last token update.  If updating after this point, disallow API accesses.
        if (
            self._access_token == ""
            or datetime.strptime(self._access_token_date_updated, "%Y-%m-%dT%H:%M:%S")
            + timedelta(minutes=45)
            < now
        ):
            _LOGGER.debug(
                "Access_token too old or not set correctly while authenticating"
            )
            return False
        else:
            # Configure other data necessary for fetching data
            try:
                self._get_customer_id()
                self._get_active_meters()
                return True
            except LoginFailed as lf:
                _LOGGER.error(
                    "Login failed during authenticate_using_access_token: %s", lf
                )
                return False
            except HTTPFailed as hf:
                _LOGGER.error(
                    "HTTP failure during authenticate_using_access_token: %s", hf
                )
                return False

    def get_customers(self, access_token=None):
        """Return the customers and addresses accessible to this login."""
        if access_token is not None:
            if len(access_token) < 1200:
                raise LoginFailed("Invalid access token")
            self._access_token = "Bearer " + access_token
        headers = {
            "Authorization": self._access_token,
        }

        url = f"{self._api_url}/api/profile/get"

        try:
            response = requests.get(url, headers=headers, timeout=self._request_timeout)
        except requests.exceptions.RequestException as req_err:
            _LOGGER.error(
                "Request error occurred while retrieving customer id: %s", req_err
            )
            # Network or other request-level error
            raise HTTPFailed from req_err

        # Handle HTTP status codes explicitly: 401/403 indicate invalid/expired token
        if response.status_code in (401, 403):
            _LOGGER.error(
                "Authentication failed when retrieving customer id: %s %s",
                response.status_code,
                response.text,
            )
            raise LoginFailed("Invalid or expired access token")

        try:
            response.raise_for_status()
        except requests.exceptions.HTTPError as http_err:
            _LOGGER.error(
                "HTTP error occurred while retrieving customer id: %s", http_err
            )
            raise HTTPFailed from http_err
        # response = requests.get(url, headers=headers)
        # self._print_json(response.json(), "Retrieved customer ID JSON response")
        # Parse and validate JSON payload
        try:
            resp_json = response.json()
            customers = resp_json["Customers"]
            if not isinstance(customers, list):
                raise ValueError("Customers must be a list")
        except Exception as json_err:
            _LOGGER.error("Failed to parse customer id response: %s", json_err)
            raise HTTPFailed from json_err
        return customers

    def _get_customer_id(self):
        customers = self.get_customers()
        self._customer_id = ""
        self._customer_number = ""
        try:
            if self._selected_customer_id is None:
                customer = customers[0]
            else:
                customer = next(
                    customer
                    for customer in customers
                    if str(customer["Id"]) == self._selected_customer_id
                )
            self._customer_id = str(customer["Id"])
            self._customer_number = str(customer["Number"])
        except (IndexError, KeyError, StopIteration, TypeError) as error:
            raise HTTPFailed("Selected customer is unavailable") from error
        _LOGGER.debug(
            "Retrieved customer_id, number: %s, %s",
            self._customer_id,
            self._customer_number,
        )

    def get_meters(self, customer_id):
        """Discover active water and heating meters for one customer."""
        self._selected_customer_id = str(customer_id)
        self._get_customer_id()
        self._get_active_meters()
        return self.get_meter_types()

    def _get_active_meters(self):
        """
        The returned data is a list of installations:
        [{'InstallationPeriodId': <int>,
          'InstallationId': <int>,     <--- this one is important
          'LocationId': <int>,
          'Location': '<str>',
          'MeasurementPointId': <int>,  <--- this one is important
          'MeasurementPointType': '',
          'MeasurementPointNumber': '<str>',
          'MeterId': <int>,
          'ConsumptionTypeId': 6,          <--- '6'=water, '5'=heating
          'ConsumptionTypeName': 'Vand',   <--- might want to ensure we are looking at water too
          'IsRemoteRead': True,
          'IsActive': True,                <--- Check this one for True
          'MeterNumber': '<int>',
          'MeterTypeId': <int>,
          'MeasurementPointTypeCodeText': '',
          'Units': [{'Id': <int>,          <-- need to save this dict as well, Id as minimum
                     'Name': 'm³',
                     'Description': 'Vand',
                     'Decimals': 0,
                     'Order': 1
                    }]
        }]
        """
        headers = {
            "Customer-Id": self._customer_id,
            "Customer-Number": self._customer_number,
            "Authorization": self._access_token,
        }

        data = {"IncludeUnits": "true"}

        url = f"{self._api_url}/api/meter/customerActiveMeters"

        try:
            response = requests.post(
                url,
                data=data,
                headers=headers,
                timeout=self._request_timeout,
            )
            if response.status_code in (401, 403):
                raise LoginFailed("Invalid or expired access token")
            response.raise_for_status()
            response_json = response.json()
            if not isinstance(response_json, list):
                raise ValueError("Meters must be a list")
        except (requests.exceptions.RequestException, ValueError) as error:
            raise HTTPFailed("Failed to retrieve active meters") from error
        # NOTE: Failure may happen right here whenever the API is updated with new headers and what not.
        # self._print_json(response.json(), "Get active meters response")

        self._active_meters = []
        self._meter_data = {}
        self._meter_data_extra = {}
        for meter in response_json:
            if (
                self._selected_measurement_point_id is not None
                and str(meter["MeasurementPointId"])
                != self._selected_measurement_point_id
            ):
                continue
            """ Pick up active water measuring meters """
            if meter["IsActive"] and meter["ConsumptionTypeId"] == 6:
                # Water type
                active = {
                    "type": "water",
                    "InstallationId": meter["InstallationId"],
                    "MeasurementPointId": meter["MeasurementPointId"],
                    "MeterTypeId": meter.get("MeterTypeId"),
                    "Unit": meter["Units"][0],
                    "Units": meter["Units"],
                }
                self._meter_data["water"] = []
                self._meter_data_extra["water"] = []
                self._active_meters.append(active)
            if meter["IsActive"] and meter["ConsumptionTypeId"] == 5:
                # Heating type
                active = {
                    "type": "heating",
                    "InstallationId": meter["InstallationId"],
                    "MeasurementPointId": meter["MeasurementPointId"],
                    "MeterTypeId": meter.get("MeterTypeId"),
                    "Unit": meter["Units"][0],
                    "Units": meter["Units"],
                }
                self._meter_data["heating"] = []
                self._meter_data_extra["heating"] = []
                self._active_meters.append(active)
        if self._selected_measurement_point_id is not None and not self._active_meters:
            raise HTTPFailed("Selected meter is unavailable")
        _LOGGER.debug("Got active (water/heating) meters : %s", self._active_meters)

    def get_meter_types(self):
        return self._active_meters

    def get_available_time_series_periods(self) -> datetime | None:
        """Get data from the API about which time series periods are available for the active meters.
        This can be used to determine how far back data is available for the statistics sensor.

        [
          {
            "MeasurementPointId":12345678,
            "UnitId":12345,
            "RangeType":0,
            "MinDate":"2022-01-01T00:00:00",
            "MaxDate":"2026-02-04T00:00:00"
          },
          {
            "MeasurementPointId":0,
            "UnitId":0,
            "RangeType":1,
            "MinDate":"2022-01-01T00:00:00",
            "MaxDate":"2026-12-31T00:00:00"
          }
        ]
        """
        headers = {
            "Customer-Id": self._customer_id,
            "Customer-Number": self._customer_number,
            "Authorization": self._access_token,
        }
        url = f"{self._api_url}/api/consumption/availableTimeSeriesPeriods"
        try:
            response = requests.get(url, headers=headers, timeout=self._request_timeout)
            response.raise_for_status()
        except requests.exceptions.RequestException as req_err:
            _LOGGER.error("Failed to retrieve available time periods: %s", req_err)
            raise HTTPFailed from req_err
        result_json = response.json()
        self._time_series_periods = result_json or []

        # Enable logging DEBUG to see all returned data from the API:
        self._print_json(
            result_json, "Retrieved available timeseries periods JSON response"
        )

        # Entry with RangeType 0 is for the specific meter, and is the one we care about.  It has the actual earliest date of data available.
        periods = result_json or []
        if self._selected_measurement_point_id is not None:
            # One config entry per meter: only that meter's periods, or the
            # account wide range when KMD lists none for it.
            periods = [
                p
                for p in periods
                if str(p.get("MeasurementPointId"))
                == self._selected_measurement_point_id
            ] or [p for p in periods if p.get("RangeType") == 1]
            if not periods:
                raise HTTPFailed("Selected meter has no available periods")
        # Entries with RangeType 0 are specific meters and carry the actual
        # earliest date of data available.
        periods = [p for p in periods if p.get("RangeType") == 0] or periods
        min_dates = [p["MinDate"] for p in periods if p.get("MinDate")]
        if not min_dates:
            _LOGGER.warning(
                "KMD API returned no available time series periods - not limiting history import"
            )
            self._earliest_data_date = None
            return None
        self._earliest_data_date = datetime.fromisoformat(min(min_dates))
        # self._earliest_data_date = self._local_str_to_utc(result_json[0]["MinDate"])
        return self._earliest_data_date

    def _get_consumption_timeseries(
        self, metering_device, dateFrom, dateTo, zoomLevel=0
    ):
        """Get the timeseries as requested for a single metering device, based on zoom level and date range.

        Zoomlevel:
         0 : Year
         1 : Month
         2 : Day
         3 : Hour
         4 : Billing Period

         Example:
         {'SheetName': None,
          'Series': [
            {'Data': [
                {'DateFrom': '2022-01-10T00:00:00+01:00',
                 'DateTo': '2022-01-10T23:59:59+01:00',
                 'Value': 0.344,  <--- has accurate measurement registered
                 'UnitName': 'm³',
                 'Label': '',
                 'IsComplete': True, <--- this one is complete
                 'IsSettlement': False
                },
                {'DateFrom': '2022-01-11T00:00:00+01:00',
                 'DateTo': '2022-01-11T23:59:59+01:00',
                 'Value': 0.01,   <--- has one measurement registered
                 'UnitName': 'm³',
                 'Label': '',
                 'IsComplete': False,  <--- but is incomplete!  This is ignored in the web interface. Data is displayed regardless.
                 'IsSettlement': False
                },
                {'DateFrom': '2022-01-12T00:00:00+01:00',
                 'DateTo': '2022-01-12T23:59:59+01:00',
                 'Value': 0.0,   <--- no measurements
                 'UnitName': 'm³',
                 'Label': '',
                 'IsComplete': False,  <--- and incomplete.  This is ignored in the web interface. Data is displayed regardless.
                 'IsSettlement': False
                }],
             'Label': None
            }
         ],
         'Total': {
             'Value': 0.354,
             'DateFrom': '2022-01-10T01:00:00+01:00',
             'DateTo': '2022-01-15T00:00:00+01:00'
             },
         'Average': {
             'Value': 0.344,
             'DateFrom': '2022-01-10T00:00:00+01:00',
             'DateTo': '2022-01-10T23:59:59+01:00'
             },
         'Maximum': {
             'Value': 0.344,
             'DateFrom': '2022-01-10T00:00:00+01:00',
             'DateTo': '2022-01-10T23:59:59+01:00'
             },
         'Minimum': {
             'Value': 0.344,
             'DateFrom': '2022-01-10T00:00:00+01:00',
             'DateTo': '2022-01-10T23:59:59+01:00'
             }
        }

        The returned data is flattened a little to only take the first data series for each meter.
        I don't know why this could be a list, so please report a bug or a pull request with a fix
        if this is not the case for you.

        Returned data has this structure:
        [
            {
                "type" : "water|heating" (so far),
                "Data" : []
                "Total": float,
                ...
            },
            ...
        ]
        """
        headers = {
            "Customer-Id": self._customer_id,
            "Customer-Number": self._customer_number,
            "Authorization": self._access_token,
        }

        # Setup query parameters for the API.
        # Necessary fields are installation relevant properties and the date/zoom range.
        data = {
            "InstallationId": metering_device["InstallationId"],
            "MeasurementPointId": metering_device["MeasurementPointId"],
            "Unit": metering_device["Unit"],
            "ZoomLevel": zoomLevel,
            "PriceData": "false",  # optional
            "Interval": "PT1H",
            "DateFrom": dateFrom,
            "DateTo": dateTo,
        }

        url = f"{self._api_url}/api/consumption/consumptionTimeSeries"

        try:
            response = requests.post(
                url,
                json=data,
                headers=headers,
                timeout=self._request_timeout,
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as req_err:
            _LOGGER.error(
                "Failed to retrieve consumption data for %s to %s: %s",
                dateFrom,
                dateTo,
                req_err,
            )
            raise HTTPFailed from req_err
        result_json = response.json()

        # Enable logging DEBUG to see all returned data from the API:
        self._print_json(result_json, "Retrieved timeseries JSON response")

        # Create dataset - only the first data series - no idea what would return more than one.
        series_data = []
        last_valid_date = ""
        if result_json["Series"]:
            for data in result_json["Series"][0]["Data"]:
                # Add data - complete or not!
                series_data.append(
                    {"DateFrom": data["DateFrom"], "Value": data["Value"]}
                )
                # NOTE: Assuming data is sorted by date - which it is
                last_valid_date = data["DateTo"]

        # Return first data series.  Unknown how more series could come from a single metering device?
        meter_data = {
            "type": metering_device["type"],
            "Data": series_data,
            "Extra": {
                "Sum": (result_json.get("Total") or {}).get("Value"),
                "Avg": (result_json.get("Average") or {}).get("Value"),
                "Max": (result_json.get("Maximum") or {}).get("Value"),
                "Min": (result_json.get("Minimum") or {}).get("Value"),
                "LastValidDate": last_valid_date,
            },
        }
        _LOGGER.debug(f"Retrieved data from API: {meter_data}")
        return meter_data

    def _get_all_consumption_timeseries(self, dateFrom, dateTo, zoomLevel=0):
        """Retrieve data from all active metering devices"""
        meter_data = []
        for active_meter in self._active_meters:
            meter_data.append(
                self._get_consumption_timeseries(
                    active_meter, dateFrom, dateTo, zoomLevel
                )
            )
        return meter_data

    def _local_to_utc(self, local_time):
        """Convert a local time to UTC time including timezone and summer(DST)/winter time offsets."""
        if local_time.tzinfo is None:
            local_time = local_time.replace(tzinfo=self.tz)
        return local_time.astimezone(ZoneInfo("UTC"))
        # TODO: Find out what is going on here.  If the system time is UTC, the lines below does what is correct.
        #       If the system time is CET or Europe/Copenhagen, the line above works.
        #
        #
        # local_time_with_timezone = local_time.astimezone(self.tz)
        # utc_offset = local_time_with_timezone.utcoffset()
        # return local_time.astimezone(ZoneInfo("UTC")) - utc_offset

    def _local_str_to_utc_str(self, local_time_str):
        """Convert a local ISO time string to UTC time strimg."""
        return self._utc_to_isostr(
            self._local_to_utc(datetime.fromisoformat(local_time_str))
        )

    def _local_str_to_utc(self, local_time_str):
        """Convert a local ISO time string to UTC time strimg."""
        return self._local_to_utc(datetime.fromisoformat(local_time_str))

    def _utc_to_isostr(self, utc_time):
        return utc_time.isoformat().replace("+00:00", "Z")

    def get_statistics(self, from_date=None) -> dict:
        """
        Retrieve statistics based on hourly data resolution from the API.

        from_date is a datetime object with the date in local time from which to start retrieving data.  All days until present day will be retrieved.
        """
        if from_date is None:
            # If no date, just return - no default behaviour
            return {}

        # Each call is a fresh snapshot. The coordinator can request statistics
        # more than once during one refresh (once per meter type), and retaining
        # the previous call's points duplicated every hour. Besides inflating
        # consumption, that made grouped statistics reject otherwise complete
        # days because they contained 48 rather than 24 samples.
        meter_types = set(self._meter_data) | {
            meter["type"] for meter in self._active_meters
        }
        self._meter_data = {meter_type: [] for meter_type in meter_types}
        self._meter_data_extra = {meter_type: [] for meter_type in meter_types}

        # Calculate date range to process - clean time settings too
        from_date_input = from_date.replace(hour=0, minute=0, second=0, microsecond=0)
        end_date_input = datetime.now().replace(
            hour=23, minute=59, second=59, microsecond=0
        )
        days_back = (end_date_input - from_date_input).days
        _LOGGER.debug(
            f"Statistics range to fetch: {from_date_input}-{end_date_input} | {days_back} day(s)"
        )

        # The old implementation made one API request per meter per day.  A
        # first import could therefore require hundreds of sequential calls.
        # Fetch bounded chunks instead, but verify each chunk: KMD can return
        # far fewer hourly points than a multi-day range asks for.  A short
        # chunk is re-fetched one day at a time, and when that recovers hours
        # the chunk missed, chunking is abandoned for the rest of this call.
        current_date = from_date_input
        end_date_exclusive = end_date_input.replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        use_chunks = self._chunk_days > 1
        while current_date < end_date_exclusive:
            step = timedelta(days=self._chunk_days if use_chunks else 1)
            next_date = min(current_date + step, end_date_exclusive)
            time_series = self._fetch_hourly_range(current_date, next_date)

            if next_date - current_date > timedelta(days=1) and self._is_truncated(
                time_series, current_date, next_date
            ):
                daily_series = []
                day = current_date
                while day < next_date:
                    daily_series.extend(
                        self._fetch_hourly_range(
                            day, min(day + timedelta(days=1), next_date)
                        )
                    )
                    day += timedelta(days=1)
                if self._covered(daily_series) > self._covered(time_series):
                    _LOGGER.info(
                        "KMD returned incomplete hourly data for a %s-day range; "
                        "fetching one day per request",
                        (next_date - current_date).days,
                    )
                    use_chunks = False
                time_series = daily_series

            for series in time_series:
                # Resolve the type for every chunk.  Retaining this value from
                # the previous loop mixed water and heating data when more
                # than one meter was active.
                meter_type = series["type"]
                if series["Data"]:
                    # If the dataset returned is not empty extend dataset
                    self._meter_data[meter_type].extend(series["Data"])
                    self._meter_data_extra[meter_type].append(series["Extra"])

            current_date = next_date

            # _LOGGER.debug(json.dumps(time_series, sort_keys = False, indent = 4))
        # Debug output only:
        _LOGGER.debug(f"Statistics data:\n{self._meter_data}")
        _LOGGER.debug(f"Statistics extra_data:\n{self._meter_data_extra}")
        for key, series_type in self._meter_data.items():
            for hour_data in series_type:
                _LOGGER.debug(f"{hour_data['DateFrom']} - {hour_data['Value']}")
        for key, series_type in self._meter_data_extra.items():
            for extra_data in series_type:
                _LOGGER.debug(
                    f"Sum/Avg/Min/Max: {extra_data['Sum']} / {extra_data['Avg']} / {extra_data['Min']} / {extra_data['Max']} | {extra_data['LastValidDate']}"
                )

        # Data structure returned:
        #  { 'water': [{'DateFrom: <date>, 'Value': <float>}, ...],
        #    'heating': [{'DateFrom: <date>, 'Value': <float>}, ...],}
        return self._meter_data

    def _fetch_hourly_range(self, start, stop):
        """Fetch hourly data for all meters from local start up to local stop."""
        dateFrom = self._utc_to_isostr(self._local_to_utc(start))
        dateTo = self._utc_to_isostr(self._local_to_utc(stop - timedelta(seconds=1)))
        _LOGGER.info("Statistics fetch (%s to %s)", dateFrom, dateTo)
        return self._get_all_consumption_timeseries(
            dateFrom=dateFrom, dateTo=dateTo, zoomLevel=self._zoom_level["Hour"]
        )

    @staticmethod
    def _covered(time_series):
        """Return (points, consumption) returned, for comparing fetches."""
        return (
            round(sum(p["Value"] or 0 for s in time_series for p in s["Data"]), 3),
            sum(len(series["Data"]) for series in time_series),
        )

    @staticmethod
    def _is_truncated(time_series, start, stop):
        """Return True if a meter's hourly data does not cover the range.

        KMD may return fewer hours than requested, or return every hour but
        with zeros after the first day.  The second case is only visible by
        comparing the hourly values with the range total KMD reports.
        """
        # Allow one hour of slack for the daylight saving time change.
        expected = (stop - start).total_seconds() / 3600 - 1
        for series in time_series:
            if len(series["Data"]) < expected:
                return True
            total = series.get("Extra", {}).get("Sum")
            if total is None:
                continue
            hourly = sum(point["Value"] or 0 for point in series["Data"])
            if abs(hourly - total) > max(0.001, abs(total) * 0.01):
                return True
        return False

    def get_detail_units(self, meter_type) -> list[dict]:
        """Return the meter's units other than the one it is billed in.

        customerActiveMeters only names some units (for a Hillerød heat meter
        MWh and MWh-F).  availableTimeSeriesPeriods lists every unit with
        hourly data for the measurement point, so both are combined.  An
        unnamed unit gets a placeholder that consumptionTimeSeries accepts.
        """
        meter = next((m for m in self._active_meters if m["type"] == meter_type), None)
        if meter is None:
            return []
        if self._time_series_periods is None:
            self.get_available_time_series_periods()
        named = {unit["Id"]: unit for unit in meter.get("Units", [])}
        listed = {
            period["UnitId"]
            for period in self._time_series_periods or []
            if period.get("RangeType") == 0
            and period.get("MeasurementPointId") == meter["MeasurementPointId"]
            and period.get("UnitId")
        }
        return [
            named.get(
                unit_id,
                {
                    "Id": unit_id,
                    "Name": "",
                    "Description": "",
                    "Decimals": 3,
                    "Order": 99,
                },
            )
            for unit_id in sorted(listed | set(named))
            if unit_id != meter["Unit"]["Id"]
        ]

    def get_unit_statistics(self, meter_type, unit, from_date) -> list[dict]:
        """Hourly values of one unit of a meter from local from_date until today.

        One request per day, for the same reason as get_statistics.
        Returns [{'DateFrom': <local iso>, 'Value': <float>}, ...].
        """
        meter = next(m for m in self._active_meters if m["type"] == meter_type)
        device = {**meter, "Unit": unit}
        day = from_date.replace(hour=0, minute=0, second=0, microsecond=0)
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        points = []
        while day < today:
            next_day = day + timedelta(days=1)
            series = self._get_consumption_timeseries(
                device,
                self._utc_to_isostr(self._local_to_utc(day)),
                self._utc_to_isostr(
                    self._local_to_utc(next_day - timedelta(seconds=1))
                ),
                self._zoom_level["Hour"],
            )
            points.extend(series["Data"])
            day = next_day
        return points

    # The cooling report counts zoom levels from 1, the consumption API from 0.
    _COOLING_ZOOM = {"Year": 1, "Month": 2, "Day": 3, "Hour": 4}
    # KMD derives an hour's temperatures from the meter's MWh-F/MWh-R and m³
    # registers, which count in steps of 0.001.  With a few litres an hour
    # one step is tens of degrees, so a low flow hour reads 0 °C or e.g.
    # 0.001 / (0.002 * 0.001163) = 430 °C.  At 0.1 m³ a step is 8.6 °C.
    _COOLING_MIN_HOURLY_VOLUME = 0.1

    @staticmethod
    def _number(value) -> float | None:
        """KMD sends missing cooling values as null or the string 'NaN'."""
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return None if number != number else number

    @classmethod
    def _cooling_values(cls, row) -> dict:
        """Name the fields of one cooling report row.

        DegreesIn/DegreesOut are the meter's supply and return temperature.
        Degrees is KMD's cooling, derived from energy and volume, so at hourly
        resolution it is coarse.  DegreesAccumulated and DegreesPenalty are
        the year-to-date return temperature and the return temperature the
        utility requires, both as of the end of the row's period.
        """
        return {
            "supply_temperature": cls._number(row.get("DegreesIn")),
            "return_temperature": cls._number(row.get("DegreesOut")),
            "cooling": cls._number(row.get("Degrees")),
            "return_temperature_year": cls._number(row.get("DegreesAccumulated")),
            "return_requirement": cls._number(row.get("DegreesPenalty")),
            "volume": cls._number(row.get("Flow")),
            "energy": cls._number(row.get("Energy")),
        }

    def _get_cooling_info(self, meter, zoom, start, stop) -> list[dict]:
        """Return the cooling report rows for local start up to local stop.

        KMD answers with one period of the requested zoom level that starts
        at DateFrom: a whole year of months, a month of days or a day of
        hours, however long the requested range is.
        """
        headers = {
            "Customer-Id": self._customer_id,
            "Customer-Number": self._customer_number,
            "Authorization": self._access_token,
        }
        data = {
            "MetertypeId": meter["MeterTypeId"],
            "MeasurementPointId": meter["MeasurementPointId"],
            "ZoomLevel": zoom,
            "DateFrom": self._utc_to_isostr(self._local_to_utc(start)),
            "DateTo": self._utc_to_isostr(
                self._local_to_utc(stop - timedelta(seconds=1))
            ),
        }
        url = f"{self._api_url}/api/cooling/getcoolinginfo"
        try:
            response = requests.post(
                url, json=data, headers=headers, timeout=self._request_timeout
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as req_err:
            _LOGGER.error(
                "Failed to retrieve cooling data for %s to %s: %s", start, stop, req_err
            )
            raise HTTPFailed from req_err
        result_json = response.json()
        self._print_json(result_json, "Retrieved cooling JSON response")
        if not result_json.get("IsSuccess", True):
            raise HTTPFailed(result_json.get("ErrorMessage"))
        return result_json.get("CoolingData") or []

    def _heating_meter(self):
        return next((m for m in self._active_meters if m["type"] == "heating"), None)

    def get_cooling_hourly(self, from_date) -> list[dict]:
        """Hourly cooling report from local from_date until today.

        Returns [{'DateFrom': <local iso>, 'supply_temperature': ..., ...}]
        with hours KMD has no reading for, or too little flow for a
        meaningful temperature, left out.  The rows only carry an
        hour number, so on the autumn daylight saving change KMD's 24 rows
        cover 25 hours and the repeated hour is lost.
        """
        meter = self._heating_meter()
        if meter is None:
            return []
        day = from_date.replace(hour=0, minute=0, second=0, microsecond=0)
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        rows = []
        while day < today:
            next_day = day + timedelta(days=1)
            for row in self._get_cooling_info(
                meter, self._COOLING_ZOOM["Hour"], day, next_day
            ):
                values = self._cooling_values(row)
                if (
                    values["return_temperature"] is None
                    or values["volume"] is None
                    or values["volume"] < self._COOLING_MIN_HOURLY_VOLUME
                ):
                    continue
                start = day + timedelta(hours=int(row["Period"]))
                rows.append({"DateFrom": start.isoformat(), **values})
            day = next_day
        return rows

    def get_cooling_summary(self) -> dict | None:
        """Return this year's cooling and the latest day KMD has data for.

        {'year': {<values>}, 'day': {<values>, 'date': 'YYYY-MM-DD'}}.  The
        year row's return temperature is the volume weighted average the
        utility settles against; its DegreesAccumulated is empty.
        """
        meter = self._heating_meter()
        if meter is None:
            return None
        now = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        year_start = now.replace(month=1, day=1)
        year = None
        for row in self._get_cooling_info(
            meter,
            self._COOLING_ZOOM["Year"],
            year_start,
            year_start.replace(year=now.year + 1),
        ):
            if str(row.get("Period")) == str(now.year):
                year = self._cooling_values(row)
                year["return_temperature_year"] = year["return_temperature"]

        # Early in a month the latest day with data is in the month before.
        day = None
        month_start = now.replace(day=1)
        for start in (month_start, (month_start - timedelta(days=1)).replace(day=1)):
            stop = (start + timedelta(days=32)).replace(day=1)
            rows = [
                row
                for row in self._get_cooling_info(
                    meter, self._COOLING_ZOOM["Day"], start, stop
                )
                if self._number(row.get("DegreesOut")) is not None
            ]
            if rows:
                day = self._cooling_values(rows[-1])
                day["date"] = (
                    start.replace(day=int(rows[-1]["Period"])).date().isoformat()
                )
                break
        return {"year": year, "day": day}

    def get_year_data(self):
        """
        Retrieve statistics for the full year from the API.

        from_date is a datetime object with the date in local time from which to start retrieving data.  All days until present day will be retrieved.

        Returns:
          { 'type': 'water',
            'Data': [{'DateFrom': '2025-01-01T00:00:00', 'Value': 6.288}],
            'Extra': {
               'Sum': 6.288, 'Avg': 0.0, 'Max': 0.0, 'Min': 0.0, 'LastValidDate': '2025-12-31T23:59:59'
            }
          }
        """
        # Calculate date range to process - clean time settings too
        from_date_input = datetime.now().replace(
            month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
        end_date_input = datetime.now().replace(
            month=12, day=31, hour=23, minute=59, second=59, microsecond=0
        )
        dateFrom = self._utc_to_isostr(self._local_to_utc(from_date_input))
        dateTo = self._utc_to_isostr(self._local_to_utc(end_date_input))

        _LOGGER.debug(
            f"Statistics year range to fetch: {from_date_input}-{end_date_input}"
        )

        time_series = self._get_all_consumption_timeseries(
            dateFrom=dateFrom, dateTo=dateTo, zoomLevel=self._zoom_level["Year"]
        )

        meter_year_data = {}
        for series in time_series:
            type = series.pop("type")
            meter_year_data[type] = series

            if self._last_valid_day:
                meter_year_data[type]["LastValidDate"] = self._last_valid_day

            if series["Data"]:
                _LOGGER.debug(f"Year Total for {type}: {series}")
                _LOGGER.debug(json.dumps(time_series, sort_keys=False, indent=4))
            else:
                _LOGGER.warning(
                    "The KMD API returned no yearly data.  Expect sensors to signal 'unavailable'"
                )
        return meter_year_data

    def get_dummy_data(self, meter_type="water"):
        return {meter_type: [{"DateFrom": None, "Value": None}]}
