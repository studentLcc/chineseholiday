"""Data update coordinator for Chinese Holiday."""
from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DEFAULT_NAME, DOMAIN
from .holiday import Holiday

_LOGGER = logging.getLogger(__name__)


class ChineseHolidayCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Fetch holiday data asynchronously and calculate entity state off the loop."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=config.get("update_interval", timedelta(hours=8)),
        )
        self.config = config
        self.holiday = Holiday(config.get("cache_path"))
        self._calculator = None

    @classmethod
    def from_config_entry(cls, hass: HomeAssistant, entry: ConfigEntry) -> ChineseHolidayCoordinator:
        """Create a coordinator from a UI config entry."""
        data = {**entry.data, **entry.options}
        notify_times = data.get("notify_times", "09:00:00")
        return cls(
            hass,
            {
                "name": data.get("name", DEFAULT_NAME),
                "notify_times": notify_times,
                "notify_script_name": data.get("notify_script_name", ""),
                "show_detail": data.get("show_detail", True),
                "solar_anniversary": data.get("solar_anniversary", {}),
                "lunar_anniversary": data.get("lunar_anniversary", {}),
                "calculate_age": data.get("calculate_age", []),
                "notify_principles": data.get("notify_principles", {}),
                "unique_id": f"{DOMAIN}_sensor",
                "entity_id": f"sensor.{DEFAULT_NAME}",
                "cache_path": hass.config.path("chineseholiday_holidays.json"),
                "update_interval": timedelta(hours=8),
            },
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Refresh remote holiday data, then build the sensor payload."""
        await self.holiday.async_refresh(self.hass)
        if self._calculator is None:
            from .sensor import ChineseHolidaySensor

            self._calculator = ChineseHolidaySensor(self)
        self._calculator._holiday = self.holiday
        await self.hass.async_add_executor_job(self._calculator._update)
        return {
            "state": self._calculator._attr_native_value,
            "attributes": dict(self._calculator.localizedAttributes),
        }
