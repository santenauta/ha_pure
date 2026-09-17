"""The Pure VMC integration."""
from __future__ import annotations

import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import PureApi
from .const import CONF_HOST, CONF_MODBUS, DEFAULT_MODBUS, DOMAIN
from .coordinator import PureCoordinator
from .modbus import PureModbus

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.FAN,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Pure from a config entry."""
    host = entry.data[CONF_HOST]
    session = async_get_clientsession(hass)
    api = PureApi(host, session)

    # Modbus TCP is optional on these panels (it can be set to RS485 instead),
    # so only rely on it when the unit actually answers.
    modbus: PureModbus | None = None
    if entry.options.get(CONF_MODBUS, DEFAULT_MODBUS):
        candidate = PureModbus(api.hostname)
        if await candidate.test_connection():
            modbus = candidate
        else:
            _LOGGER.info(
                "%s does not answer Modbus TCP; reading the web pages instead",
                api.hostname,
            )

    coordinator = PureCoordinator(hass, entry, api, modbus)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when the options change, so the data source is picked again."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
