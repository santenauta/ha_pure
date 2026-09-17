"""The free cooling switch: holding a seasonal bypass closed through the season."""
from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.pure.const import CONF_HOST, DOMAIN

from .conftest import FakePanel
from .test_init import HOST, _modbus_on

pytestmark = pytest.mark.enable_socket

SWITCH = "switch.pure_vmc_free_cooling"
SUMMER, WINTER = 2, 1


def _set_temperatures(panel: FakePanel, outside: float, inside: float) -> None:
    panel.registers[81] = int(outside * 10)
    panel.registers[82] = int(inside * 10)


@pytest.fixture
async def entry(hass: HomeAssistant, panel: FakePanel, socket_enabled) -> MockConfigEntry:
    panel.registers[51] = 40
    panel.registers[87] = panel.registers[88] = 1200  # fans running
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: HOST})
    entry.add_to_hass(hass)
    with _modbus_on(panel):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def _refresh(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.data[DOMAIN][entry.entry_id].async_refresh()
    await hass.async_block_till_done()


async def _turn(hass: HomeAssistant, service: str) -> None:
    await hass.services.async_call("switch", service, {"entity_id": SWITCH}, blocking=True)
    await hass.async_block_till_done()


async def test_starts_on_in_summer(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert hass.states.get(SWITCH).state == "on"
    assert hass.states.get(SWITCH).attributes["season"] == "summer"


async def test_off_holds_the_bypass_closed(
    hass: HomeAssistant, panel: FakePanel, entry: MockConfigEntry
) -> None:
    # A summer night: outside colder than inside. "summer" would open the bypass,
    # so holding it closed means moving to "winter".
    _set_temperatures(panel, outside=19.0, inside=25.0)
    await _refresh(hass, entry)
    await _turn(hass, "turn_off")
    assert panel.registers[20] & 0b11 == WINTER
    assert panel.stored[-1] == 0b01  # a configuration change, stored

    # On "winter" the unit still free-cools a house above its winter set-point,
    # so on the next poll that set-point goes out of reach
    assert panel.registers[52] == 260
    await _refresh(hass, entry)
    assert panel.registers[52] == 300
    assert panel.stored[-1] == 0b10

    # Morning: outside becomes warmer than inside. "winter" would now open the
    # bypass for free heating, so it has to be "summer" again - but not more
    # often than the minimum interval allows.
    _set_temperatures(panel, outside=28.0, inside=25.0)
    await _refresh(hass, entry)
    assert panel.registers[20] & 0b11 == WINTER
    with patch("custom_components.pure.switch.MIN_SEASON_CHANGE_INTERVAL", 0):
        await _refresh(hass, entry)
    assert panel.registers[20] & 0b11 == SUMMER
    assert hass.states.get(SWITCH).state == "off"


async def test_dead_band_and_stopped_fans_change_nothing(
    hass: HomeAssistant, panel: FakePanel, entry: MockConfigEntry
) -> None:
    with patch("custom_components.pure.switch.MIN_SEASON_CHANGE_INTERVAL", 0):
        _set_temperatures(panel, outside=24.8, inside=25.0)  # within the hysteresis
        await _refresh(hass, entry)
        await _turn(hass, "turn_off")
        assert panel.registers[20] & 0b11 == SUMMER

        # With the fans stopped the probes only measure the ducts
        panel.registers[87] = 0
        _set_temperatures(panel, outside=15.0, inside=25.0)
        await _refresh(hass, entry)
        assert panel.registers[20] & 0b11 == SUMMER


async def test_setpoint_is_left_alone_with_a_heater(
    hass: HomeAssistant, panel: FakePanel, socket_enabled
) -> None:
    panel.registers[7] = 1  # an electric post-heater: the set-point is its target
    panel.registers[87] = 1200
    _set_temperatures(panel, outside=19.0, inside=25.0)
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: HOST})
    entry.add_to_hass(hass)
    with _modbus_on(panel):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    await _turn(hass, "turn_off")
    await _refresh(hass, entry)
    assert panel.registers[20] & 0b11 == WINTER
    assert panel.registers[52] == 260


async def test_on_returns_to_summer(
    hass: HomeAssistant, panel: FakePanel, entry: MockConfigEntry
) -> None:
    _set_temperatures(panel, outside=19.0, inside=25.0)
    await _refresh(hass, entry)
    await _turn(hass, "turn_off")
    assert panel.registers[20] & 0b11 == WINTER
    await _turn(hass, "turn_on")  # a command is never held back by the interval
    assert panel.registers[20] & 0b11 == SUMMER
    assert panel.registers[20] == 34  # the other bits of the register untouched


async def test_not_offered_for_a_universal_bypass(
    hass: HomeAssistant, panel: FakePanel, socket_enabled
) -> None:
    panel.registers[7] = 2 << 10
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_HOST: HOST})
    entry.add_to_hass(hass)
    with _modbus_on(panel):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert hass.states.get(SWITCH) is None
    assert hass.states.get("select.pure_vmc_bypass_mode") is not None
