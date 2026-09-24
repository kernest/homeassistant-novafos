"""Sensor platform for Novafos integration."""

from __future__ import annotations
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.const import CONF_NAME
from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.typing import StateType
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import (
    DOMAIN,
    WATER_SENSOR_TYPES,
    HEATING_SENSOR_TYPES,
    EXTRA_WATER_SENSOR_TYPES,
    EXTRA_HEATING_SENSOR_TYPES,
)

from .model import NovafosSensorDescription

import logging

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, config: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the sensor platform."""
    # Use the name for the unique id of each sensor. novafos_<supplierid>?
    name: str = config.data[CONF_NAME]
    coordinator: DataUpdateCoordinator = hass.data[DOMAIN][config.entry_id][
        "coordinator"
    ]
    # coordinator has a 'data' field.  This is set to the returned API data value.
    # _async_update_data updates the field.
    # From this field the sensors will get their values afterwards.

    # The sensors are defined in the const.py file
    sensors: list[NovafosWaterSensor] = []
    # The coordinator data is already populated and this means it is possible to 'auto-discover' which sensors to create:
    # If the first refresh failed coordinator.data is None.  Fall back to the
    # meters discovered during authentication so the entities still get created
    # and become available once a later refresh succeeds.
    if coordinator.data is not None:
        meter_types = set(coordinator.data[0])
    else:
        meter_types = {meter["type"] for meter in coordinator.api.get_meter_types()}

    if "water" in meter_types:
        for description in WATER_SENSOR_TYPES:
            sensors.append(NovafosWaterSensor(name, coordinator, description))
            # _LOGGER.debug("Adding Novafos sensor %s", description.name)
        if config.data["use_grouped_sensors"]:
            for description in EXTRA_WATER_SENSOR_TYPES:
                sensors.append(NovafosWaterSensor(name, coordinator, description))
                # _LOGGER.debug("Adding Novafos sensor %s", description.name)

    if "heating" in meter_types:
        for description in HEATING_SENSOR_TYPES:
            sensors.append(NovafosWaterSensor(name, coordinator, description))
        if config.data["use_grouped_sensors"]:
            for description in EXTRA_HEATING_SENSOR_TYPES:
                sensors.append(NovafosWaterSensor(name, coordinator, description))

    async_add_entities(sensors)


class NovafosWaterSensor(CoordinatorEntity, SensorEntity):
    """Representation of a Sensor."""

    entity_description: NovafosSensorDescription

    def __init__(self, name, coordinator, description):
        """Initialise the coordinator"""
        super().__init__(coordinator)

        """Initialize the sensor."""
        self.entity_description = description
        self._attrs: dict[str, Any] = {}

        _LOGGER.debug(f"Registering Sensor for {description.name}")

        self._attr_name = f"{name} {description.name}"
        self._attr_unique_id = (
            f"{name.lower()}-{description.sensor_type}-{description.key}"
        )

        # Note: Data is stored in self.coordinator.data

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes.
        Attributes could include the last total yearly consumption.
        This value may be used in a template sensor which can in turn be
        used in the energy dashboard.
        TODO: If yearly values should be included it is probably a good idea
              to just make that one extra REST API call and the the latest
              year total.  Alternately the sum of all hours since start-of-year
              is to be calculated on every fetch. That is many data points
              at the end of the year.
        """
        _LOGGER.debug(self.coordinator.data)
        if self.coordinator.data is None:
            self._attrs = {}
        elif self.entity_description.key == "hourly":
            readings = self.coordinator.data[0].get(
                self.entity_description.sensor_type, []
            )
            if readings:
                self._attrs = {"reading_start": readings[-1]["DateFrom"]}
            else:
                self._attrs = {}
        elif (
            self.entity_description.key == "statistics"
            and self.coordinator.data[1] is not None
        ):
            self._attrs = {}
            year_data = (
                self.coordinator.data[1]
                .get(self.entity_description.sensor_type, {})
                .get("Data", [])
            )
            if year_data:
                self._attrs["year_total"] = year_data[-1]["Value"]
        #     self._attrs["last_valid_date"] = self.coordinator.data[self.entity_description.sensor_type][self.entity_description.key]["LastValidDate"]
        else:
            self._attrs = {}
        return self._attrs

        # self._attrs = {}

    @property
    def native_value(self) -> StateType:
        """Return the latest hourly reading or cumulative recorder total."""
        if self.coordinator.data is None:
            return None
        if self.entity_description.key == "hourly":
            readings = self.coordinator.data[0].get(
                self.entity_description.sensor_type, []
            )
            if readings:
                return readings[-1]["Value"]
        elif self.entity_description.key == "statistics":
            return self.coordinator.cumulative_totals.get(
                self.entity_description.sensor_type
            )
        return None
