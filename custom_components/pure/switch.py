"""Switch entities for Pure VMC (free cooling, over Modbus)."""
from __future__ import annotations

import logging
from time import monotonic
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .coordinator import PureCoordinator
from .entity_base import PureEntity
from .modbus import REG_PARAMETER_FLAGS, SEASON_MASK, SEASONS, PureModbusError

_LOGGER = logging.getLogger(__name__)

# Outside must be this much colder or warmer than inside before the season is
# changed to keep the bypass closed, and changes are spaced out: each one is a
# write to the unit's non-volatile memory.
TEMPERATURE_HYSTERESIS = 0.5  # °C
MIN_SEASON_CHANGE_INTERVAL = 15 * 60  # seconds


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: PureCoordinator = hass.data[DOMAIN][entry.entry_id]
    # Needs the season, which only Modbus reaches. A "universal" bypass is not
    # ruled by the season at all, and has its own bypass mode select instead.
    if coordinator.modbus is None or coordinator.data.get("bypass_mode_selectable"):
        return
    async_add_entities([PureFreeCoolingSwitch(coordinator, entry.entry_id)])


class PureFreeCoolingSwitch(PureEntity, SwitchEntity, RestoreEntity):
    """Lets a thermostat decide when the bypass may bring outside air in.

    A seasonal bypass cannot be commanded. The unit opens it by itself: in
    summer when outside is colder than inside (free cooling), in winter when
    outside is warmer (free heating). It has no target temperature though, so
    on a summer night it keeps cooling the house well past comfortable.

      on  -> season "summer": the unit free-cools whenever it can.
      off -> the bypass is held closed, by keeping the unit on the season whose
             condition is not met: "winter" while outside is colder than
             inside, "summer" while it is warmer. The heat exchanger then keeps
             the house as it is.

    The temperatures are only meaningful while the fans run; with the fans
    stopped nothing is changed when off.
    """

    _attr_translation_key = "free_cooling"
    _attr_icon = "mdi:snowflake-thermometer"

    def __init__(self, coordinator: PureCoordinator, entry_id: str) -> None:
        super().__init__(coordinator, entry_id)
        self._attr_unique_id = f"{entry_id}_free_cooling"
        self._allowed = coordinator.data.get("season") != "winter"
        self._last_change: float | None = None
        self._applying = False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            self._allowed = last_state.state == STATE_ON

    @property
    def is_on(self) -> bool:
        return self._allowed

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"season": self.coordinator.data.get("season")}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set_allowed(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set_allowed(False)

    async def _async_set_allowed(self, allowed: bool) -> None:
        self._allowed = allowed
        self.async_write_ha_state()
        try:
            await self._async_apply(commanded=True)
        except PureModbusError as err:
            raise HomeAssistantError(str(err)) from err

    @callback
    def _handle_coordinator_update(self) -> None:
        super()._handle_coordinator_update()
        # While off, the season has to follow the outside temperature
        if not self._allowed and self._wanted_season() is not None:
            self.hass.async_create_task(self._async_follow())

    async def _async_follow(self) -> None:
        try:
            await self._async_apply(commanded=False)
        except PureModbusError as err:
            _LOGGER.warning("Could not change the season to hold the bypass: %s", err)

    def _wanted_season(self) -> str | None:
        """The season to be on, or None when the current one can stay."""
        data = self.coordinator.data
        if self._allowed:
            wanted = "summer"
        else:
            outside, inside = data.get("temp_external"), data.get("temp_return")
            if not data.get("fan_supply_speed") or outside is None or inside is None:
                return None
            if outside < inside - TEMPERATURE_HYSTERESIS:
                wanted = "winter"
            elif outside > inside + TEMPERATURE_HYSTERESIS:
                wanted = "summer"
            else:
                return None
        return None if data.get("season") == wanted else wanted

    async def _async_apply(self, commanded: bool) -> None:
        if self._applying:
            return
        wanted = self._wanted_season()
        if wanted is None:
            return
        now = monotonic()
        if (
            not commanded
            and self._last_change is not None
            and now - self._last_change < MIN_SEASON_CHANGE_INTERVAL
        ):
            return
        self._applying = True
        try:
            raw = next(raw for raw, name in SEASONS.items() if name == wanted)
            self._last_change = now
            await self.coordinator.async_write_register(
                REG_PARAMETER_FLAGS, raw, SEASON_MASK
            )
        finally:
            self._applying = False
