"""Chinese Holiday integration."""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.helpers import config_validation as cv, service
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN, PLATFORMS, SERVICE_GET, SERVICE_SET_MEMORIAL
from .coordinator import ChineseHolidayCoordinator

_LOGGER = logging.getLogger(__name__)
ENTITY_SERVICE_SCHEMA = {vol.Optional("force", default=True): cv.boolean}
MEMORIAL_SERVICE_SCHEMA = {
    vol.Required("name"): cv.string,
    vol.Required("calendar"): vol.In(["solar", "lunar"]),
    vol.Required("date"): cv.string,
    vol.Optional("mode", default="yearly_count"): vol.In(["yearly", "yearly_count", "timer"]),
    vol.Optional("operation", default="add"): vol.In(["add", "delete"]),
}


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the entity-targeted manual holiday fetch action."""
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_GET,
        entity_domain="sensor",
        schema=ENTITY_SERVICE_SCHEMA,
        func="async_get_holidays",
        supports_response=SupportsResponse.OPTIONAL,
    )
    service.async_register_platform_entity_service(
        hass,
        DOMAIN,
        SERVICE_SET_MEMORIAL,
        entity_domain="sensor",
        schema=MEMORIAL_SERVICE_SCHEMA,
        func="async_set_memorial",
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


def _migrate_sensor_unique_id(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Preserve the existing entity_id while moving to a per-entry unique_id."""
    registry = er.async_get(hass)
    old_unique_id = f"{DOMAIN}_sensor"
    new_unique_id = f"{DOMAIN}_{entry.entry_id}"
    if old_unique_id == new_unique_id:
        return
    for entity in registry.entities.values():
        if (
            entity.platform == DOMAIN
            and entity.unique_id == old_unique_id
            and entity.config_entry_id == entry.entry_id
        ):
            try:
                registry.async_update_entity(entity.entity_id, new_unique_id=new_unique_id)
            except ValueError:
                _LOGGER.exception("无法迁移传感器实体 ID：%s", entity.entity_id)
            return


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Chinese Holiday from a config entry."""
    _migrate_sensor_unique_id(hass, entry)
    coordinator = ChineseHolidayCoordinator.from_config_entry(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options."""
    await hass.config_entries.async_reload(entry.entry_id)
