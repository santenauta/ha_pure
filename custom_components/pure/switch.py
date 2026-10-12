"""Switch entities for Pure VMC (free cooling and free heating, over Modbus)."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
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
from .modbus import (
    REG_PARAMETER_FLAGS,
    REG_TEMP_SETPOINT,
    SEASON_MASK,
    SEASONS,
    PureModbusError,
)

_LOGGER = logging.getLogger(__name__)

# Outside must be this much colder or warmer than inside before the season is
# changed to keep the bypass closed, and changes are spaced out: each one is a
# write to the unit's non-volatile memory.
TEMPERATURE_HYSTERESIS = 0.5  # °C
MIN_SEASON_CHANGE_INTERVAL = 15 * 60  # seconds

# In "winter" the unit still opens the bypass to cool a house that is above its
# winter set-point. Holding the bypass closed therefore also takes a winter
# set-point no house reaches: the highest one the unit accepts.
HOLD_SETPOINT = 30.0  # °C

# In "summer" the unit may also want the house above the summer set-point
# before it opens the bypass: a Pure 250 on summer with set-point 26 kept it
# closed for 14 minutes at 24 °C inside, 21.5 °C outside, and opened it three
# minutes after the set-point went to 20 (it then stayed open with 26 again,
# so this is not certain). With free cooling on, the thermostat decides when
# the house is cool enough, so the summer set-point goes low; not lower than
# this, so a unit left on its own does not cool the house into the cold.
COOL_SETPOINT = 20.0  # °C

COOLING = "cooling"
HEATING = "heating"


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
    control = BypassControl(hass, coordinator)
    async_add_entities(
        [
            PureBypassSwitch(coordinator, entry.entry_id, control, COOLING),
            PureBypassSwitch(coordinator, entry.entry_id, control, HEATING),
        ]
    )


class BypassControl:
    """Lets a thermostat decide when the bypass may bring outside air in.

    A seasonal bypass cannot be commanded. The unit opens it by itself, as
    observed on a Pure 250 (the manual tells only half of it):

      summer -> when outside is colder than inside (and, it seems, the house
                above the summer set-point): on a summer night it cools the
                house well past comfortable;
      winter -> when outside is warmer than inside (free heating), and also
                when outside is colder while the house is above the winter
                set-point. The set-point is stored per season.

    It reacts one to four minutes after a change. So, with two switches of
    which at most one is on:

      free cooling on -> season "summer" with the summer set-point low: the
                         unit free-cools whenever outside is colder.
      free heating on -> season "winter" with the winter set-point at its
                         maximum: the bypass opens only while outside is warmer.
      both off        -> the bypass is held closed: "winter" with the winter
                         set-point at its maximum while outside is colder than
                         inside, "summer" while it is warmer. The heat exchanger
                         then keeps the house as it is.

    The temperatures are only meaningful while the fans run; with the fans
    stopped the season is left alone. The set-point is left alone on units that
    report a post-heater, where it is what the heater aims for.
    """

    def __init__(self, hass: HomeAssistant, coordinator: PureCoordinator) -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.mode: str | None = (
            COOLING if coordinator.data.get("season") != "winter" else None
        )
        self._last_change: float | None = None
        self._lock = asyncio.Lock()
        self._following = False
        self._listeners: list[Callable[[], None]] = []

    @callback
    def add_listener(self, listener: Callable[[], None]) -> None:
        self._listeners.append(listener)

    @callback
    def restore(self, mode: str, was_on: bool) -> None:
        """Take back what a switch was before a restart."""
        if was_on:
            self.mode = mode
        elif self.mode == mode:
            self.mode = None

    async def async_set_mode(self, mode: str | None) -> None:
        self.mode = mode
        for listener in self._listeners:
            listener()
        try:
            async with self._lock:
                await self._async_apply(commanded=True)
        except PureModbusError as err:
            raise HomeAssistantError(str(err)) from err

    @callback
    def follow(self) -> None:
        """After a poll: keep the season and set-point where the mode needs them."""
        if self._following or self._lock.locked():
            return
        if self._wanted_season(following=True) is None and self._wanted_setpoint() is None:
            return
        self._following = True
        self.hass.async_create_task(self._async_follow())

    async def _async_follow(self) -> None:
        try:
            async with self._lock:
                await self._async_apply(commanded=False)
        except PureModbusError as err:
            _LOGGER.warning("Could not change the season to hold the bypass: %s", err)
        finally:
            self._following = False

    def _wanted_season(self, following: bool = False) -> str | None:
        """The season to be on, or None when the current one can stay.

        A switch that is on sets its season when it is turned on; only while
        both are off does the season keep following the outside temperature.
        """
        data = self.coordinator.data
        if self.mode is not None:
            if following:
                return None
            wanted = "summer" if self.mode == COOLING else "winter"
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

    def _wanted_setpoint(self) -> float | None:
        """The set-point the current season needs, or None when it can stay.

        Out of reach in winter, so the unit never opens the bypass to cool;
        low in summer while free cooling is on, so the thermostat decides.

        The register shows the set-point of the season the unit is on, and only
        settles a moment after a season change: so this is looked at on the
        polls that follow one, never in the same breath.
        """
        data = self.coordinator.data
        setpoint = data.get("temp_setpoint")
        if setpoint is None or data.get("heater_fitted"):
            return None
        season = data.get("season")
        if self.mode == COOLING:
            if season == "summer" and setpoint > COOL_SETPOINT:
                return COOL_SETPOINT
        elif season == "winter" and setpoint < HOLD_SETPOINT:
            return HOLD_SETPOINT
        return None

    async def _async_apply(self, commanded: bool) -> None:
        wanted = self._wanted_season()
        if wanted is None:
            if (setpoint := self._wanted_setpoint()) is not None:
                await self.coordinator.async_write_register(
                    REG_TEMP_SETPOINT, int(setpoint * 10)
                )
            return
        now = monotonic()
        if (
            not commanded
            and self._last_change is not None
            and now - self._last_change < MIN_SEASON_CHANGE_INTERVAL
        ):
            return
        raw = next(raw for raw, name in SEASONS.items() if name == wanted)
        self._last_change = now
        await self.coordinator.async_write_register(
            REG_PARAMETER_FLAGS, raw, SEASON_MASK
        )


class PureBypassSwitch(PureEntity, SwitchEntity, RestoreEntity):
    """Free cooling or free heating: whether the bypass may open to cool or to heat.

    Turning one on turns the other off. Wired as the cooler and the heater of
    a Home Assistant thermostat, the bypass then opens only while the house is
    away from the wanted temperature and outside air brings it closer.
    """

    def __init__(
        self,
        coordinator: PureCoordinator,
        entry_id: str,
        control: BypassControl,
        mode: str,
    ) -> None:
        super().__init__(coordinator, entry_id)
        self._control = control
        self._mode = mode
        self._attr_translation_key = f"free_{mode}"
        self._attr_unique_id = f"{entry_id}_free_{mode}"
        self._attr_icon = (
            "mdi:snowflake-thermometer" if mode == COOLING else "mdi:sun-thermometer"
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last_state := await self.async_get_last_state()) is not None:
            self._control.restore(self._mode, last_state.state == STATE_ON)
        self._control.add_listener(self.async_write_ha_state)

    @property
    def is_on(self) -> bool:
        return self._control.mode == self._mode

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"season": self.coordinator.data.get("season")}

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._control.async_set_mode(self._mode)

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self.is_on:
            await self._control.async_set_mode(None)

    @callback
    def _handle_coordinator_update(self) -> None:
        super()._handle_coordinator_update()
        self._control.follow()
