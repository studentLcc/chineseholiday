"""Chinese Holiday integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN, PLATFORMS, SERVICE_GET
from .coordinator import ChineseHolidayCoordinator

_LOGGER = logging.getLogger(__name__)

GET_SCHEMA = vol.Schema(
    {
        vol.Optional("force", default=True): cv.boolean,
    }
)


def async_register_coordinator(
    hass: HomeAssistant, key: str, coordinator: ChineseHolidayCoordinator
) -> None:
    """Remember a coordinator so the manual refresh action can find it."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("coordinators", {})[key] = coordinator


def async_unregister_coordinator(hass: HomeAssistant, key: str) -> None:
    """Drop a coordinator when its config entry unloads."""
    store = hass.data.get(DOMAIN, {}).get("coordinators", {})
    store.pop(key, None)


def _coordinators(hass: HomeAssistant) -> list[ChineseHolidayCoordinator]:
    store = hass.data.get(DOMAIN, {}).get("coordinators", {})
    return [item for item in store.values() if item is not None]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up YAML compatibility and the manual holiday fetch action."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN].setdefault("coordinators", {})

    async def handle_get(call: ServiceCall) -> dict[str, Any] | None:
        """Pull holiday data now, without changing anniversaries or holidays config."""
        force = bool(call.data.get("force", True))
        coordinators = _coordinators(hass)
        if not coordinators:
            _LOGGER.warning("chineseholiday.get: 没有已加载的节假日实例")
            return {"results": []} if call.return_response else None

        _LOGGER.info("手动拉取节假日，force=%s，实例数=%s", force, len(coordinators))
        results: list[dict[str, Any]] = []
        for coordinator in coordinators:
            try:
                results.append(await coordinator.async_get_holidays(force))
            except Exception as err:
                _LOGGER.exception("chineseholiday.get 拉取失败: %s", err)
                results.append(
                    {
                        "name": coordinator.config.get("name"),
                        "error": str(err),
                        "forced": force,
                    }
                )
        if call.return_response:
            return {"results": results}
        return None

    hass.services.async_register(
        DOMAIN,
        SERVICE_GET,
        handle_get,
        schema=GET_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Chinese Holiday from a config entry."""
    coordinator = ChineseHolidayCoordinator.from_config_entry(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    async_register_coordinator(hass, entry.entry_id, coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    entry.async_on_unload(lambda: async_unregister_coordinator(hass, entry.entry_id))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options."""
    await hass.config_entries.async_reload(entry.entry_id)
