"""Chinese Holiday integration."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.helpers import config_validation as cv, service

from .const import DOMAIN, PLATFORMS, SERVICE_GET
from .coordinator import ChineseHolidayCoordinator

ENTITY_SERVICE_SCHEMA = {vol.Optional("force", default=True): cv.boolean}


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
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Chinese Holiday from a config entry."""
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
