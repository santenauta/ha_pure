"""Minimal Modbus TCP reader for the Pure VMC touch panel.

The panel (an EVO-PH / X511 controller) exposes its whole state as holding
registers on TCP port 502. Reading them is cheaper and far more complete than
scraping the web pages: three requests return every temperature, both fan
speeds, the run-hours counter, the individual alarms, the bypass state and the
season — none of which disturbs the wall panel.

This module deliberately has no third-party dependency (Home Assistant pins its
own pymodbus version, which custom integrations tend to clash with) and no Home
Assistant import, so it can be exercised on its own.

Register numbers below are the ones printed in the manufacturer's manual; the
address on the wire is ``register - 1``.
"""
from __future__ import annotations

import asyncio
import struct
from typing import Any

DEFAULT_PORT = 502
REQUEST_TIMEOUT = 5  # seconds

FUNCTION_READ_HOLDING = 0x03
FUNCTION_WRITE_SINGLE = 0x06

# The panel needs a moment before a written value can be read back
WRITE_SETTLE_TIME = 0.4  # seconds

# (first register, count) — contiguous blocks that cover everything we decode.
READ_BLOCKS: tuple[tuple[int, int], ...] = (
    (1, 27),   # software version, configuration, installer parameters, season/bypass, filter hours
    (51, 4),   # speed set-point, temperature set-point, boost timer
    (81, 19),  # unit 1: temperatures, status flags, fan speeds, alarms, run-hours
)

REG_SW_VERSION_YEAR_MONTH = 3
REG_SW_VERSION_DAY_PATCH = 4
REG_REMOTE_CONTROL = 5
REG_CONFIG_FLAGS_1 = 7
REG_CONFIG_FLAGS_2 = 8
REG_SPEED_BALANCE = 14
REG_PARAMETER_FLAGS = 20
REG_FILTER_MAX_HOURS = 24
REG_SPEED_SETPOINT = 51
REG_TEMP_SETPOINT = 52
REG_BOOST_TIMER = 53
REG_TEMP_EXTERNAL = 81
REG_TEMP_RETURN = 82
REG_TEMP_EXHAUST = 83
REG_TEMP_INLET = 84
REG_STATUS_FLAGS = 86
REG_FAN_SUPPLY_SPEED = 87
REG_FAN_EXHAUST_SPEED = 88
REG_ALARMS_1 = 90
REG_FAN_HOURS_HIGH = 95
REG_FAN_HOURS_LOW = 96
REG_ALARMS_2 = 97

# REMOTE_CONTROL (register 5) bits. A value written over Modbus is temporary:
# the unit goes back to the saved one after about a minute without register
# access (so at the latest when Home Assistant restarts), unless it is stored
# with one of the two "write pending" bits, which clear themselves once done.
REMOTE_DEVICE_RESET = 1 << 13  # restarts the controller: never set
REMOTE_STORE_CONFIG = 1 << 14  # stores the configuration registers (1-38)
REMOTE_STORE_SETPOINTS = 1 << 15  # stores the command registers (51-54)
FIRST_COMMAND_REGISTER = 51

# ALARMS1 (register 90) bits
ALARM1_COMM_X540 = 0
ALARM1_TEMP_EXTERNAL = 1
ALARM1_TEMP_RETURN = 2
ALARM1_TEMP_EXHAUST = 3
ALARM1_FILTERS = 4
ALARM1_FANS = 5
ALARM1_TEMP_INLET = 7
ALARM1_COMM_X531 = 8

# ALARMS2 (register 97) bits
ALARM2_CONFIGURATION = 0
ALARM2_ANTI_FROST = 1

# STATUS_FLAGS (register 86) bits
STATUS_BYPASS_OPEN = 0
STATUS_ANTI_FROST_ACTIVE = 4

# CONFIG_FLAGS_1 (register 7): bits 8-9 == 2 means the fans report a tacho signal
FANS_FAIL_TACH = 2

# CONFIG_FLAGS_1 (register 7): bits 10-11 == 2 means a "universal" bypass, the
# only kind whose mode (auto/off/on) can be chosen; otherwise the unit ignores it
BYPASS_UNIVERSAL_ON_OFF = 2

# Factory configuration, CONFIG_FLAGS_1 (register 7): (name, first bit, bit count,
# {raw value: state}). Read-only over Modbus, set in the panel's Factory menu.
FACTORY_CONFIG_1: tuple[tuple[str, int, int, dict[int, str]], ...] = (
    (
        "heater_type",
        0,
        3,
        {
            0: "none",
            1: "electric",
            2: "electric_two_stage",
            3: "water",
            4: "electric_modulating",
            5: "water_modulating",
        },
    ),
    ("regulation_probe", 4, 2, {0: "none", 1: "air_quality", 2: "humidity", 3: "external_signal"}),
    ("filter_alarm_type", 6, 2, {0: "none", 1: "pressure_switch", 2: "hours"}),
    ("fan_alarm_type", 8, 2, {0: "none", 1: "pressure_switch", 2: "tacho"}),
    ("bypass_type", 10, 2, {0: "none", 1: "season", 2: "universal"}),
    ("anti_frost_type", 12, 2, {0: "none", 1: "fan_speed", 2: "heater"}),
)
# CONFIG_FLAGS_2 (register 8) bits 0-1: the controller boards fitted
BOARD_MODELS = {0: "x540_u0_u2", 1: "x540_u3_u4", 2: "x540_u3_u4_x531"}

# SPEED_BALANCE (register 14, installer menu): extract fan speed as a percentage
# of the supply fan's. Read-only here: a Pure 250 ignores Modbus writes to it (and
# to the filter-hours threshold) while accepting the season, set-points and speed.
SPEED_BALANCE_MIN = 74
SPEED_BALANCE_MAX = 135

# PARAMETER_FLAGS (register 20): season in bits 0-1, bypass mode in bits 2-3
SEASON_MASK = 0b0011
BYPASS_MODE_MASK = 0b1100
BYPASS_MODE_SHIFT = 2

# The filter-hours threshold is stored in steps of 500 h
FILTER_HOURS_STEP = 500

# The temperature set-point reads as "off" at or below this raw value
TEMP_SETPOINT_OFF_MAX = 48

SPEED_SETPOINT_TIMER = 101  # weekly programme ("Orologio")
SPEED_SETPOINT_AUTO = 102   # air-quality / humidity probe

SEASONS = {0: "auto", 1: "winter", 2: "summer"}
BYPASS_MODES = {0: "auto", 1: "off", 2: "on"}


class PureModbusError(Exception):
    """Raised when the Modbus exchange with the panel fails."""


class PureModbusWriteIgnored(PureModbusError):
    """Raised when the panel acknowledged a write but kept the old value.

    The panel does this, without any error, for 60 s after every change made
    from its web pages or (presumably) its touch screen.
    """


class PureModbus:
    """Reads holding registers from the panel over Modbus TCP.

    The panel closes a connection after ~10 s without register access and only
    accepts a handful of them, so every read opens a connection, fetches the
    blocks and closes it again instead of keeping one alive between polls.
    """

    def __init__(self, host: str, port: int = DEFAULT_PORT, unit_id: int = 1) -> None:
        self._host = host
        self._port = port
        self._unit_id = unit_id
        self._lock = asyncio.Lock()
        self._transaction = 0

    async def read_registers(
        self, blocks: tuple[tuple[int, int], ...] = READ_BLOCKS
    ) -> dict[int, int]:
        """Return ``{register number: raw 16-bit value}`` for the given blocks."""
        async with self._lock:
            try:
                return await asyncio.wait_for(
                    self._read_blocks(blocks), timeout=REQUEST_TIMEOUT * len(blocks)
                )
            except asyncio.TimeoutError as err:
                raise PureModbusError(
                    f"Timeout talking Modbus to {self._host}:{self._port}"
                ) from err
            except (OSError, asyncio.IncompleteReadError) as err:
                raise PureModbusError(
                    f"Modbus connection to {self._host}:{self._port} failed: {err}"
                ) from err

    async def _read_blocks(
        self, blocks: tuple[tuple[int, int], ...]
    ) -> dict[int, int]:
        reader, writer = await asyncio.open_connection(self._host, self._port)
        try:
            registers: dict[int, int] = {}
            for first, count in blocks:
                values = await self._read_holding(reader, writer, first - 1, count)
                registers.update(
                    {first + offset: value for offset, value in enumerate(values)}
                )
            return registers
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def _exchange(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, pdu: bytes
    ) -> bytes:
        self._transaction = (self._transaction + 1) & 0xFFFF
        writer.write(
            struct.pack(">HHHB", self._transaction, 0, len(pdu) + 1, self._unit_id) + pdu
        )
        await writer.drain()

        header = await reader.readexactly(7)
        transaction, _protocol, length, _unit = struct.unpack(">HHHB", header)
        body = await reader.readexactly(length - 1)

        if transaction != self._transaction:
            raise PureModbusError("Modbus transaction id mismatch")
        if body[0] & 0x80:
            raise PureModbusError(f"Modbus exception code {body[1]}")
        if body[0] != pdu[0]:
            raise PureModbusError("Unexpected Modbus response")
        return body

    async def _read_holding(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        address: int,
        count: int,
    ) -> tuple[int, ...]:
        body = await self._exchange(
            reader, writer, struct.pack(">BHH", FUNCTION_READ_HOLDING, address, count)
        )
        if body[1] != count * 2:
            raise PureModbusError("Unexpected Modbus response")
        return struct.unpack(f">{count}H", body[2 : 2 + count * 2])

    async def write_register(
        self, register: int, value: int, mask: int = 0xFFFF, store: bool = True
    ) -> int:
        """Write ``value`` into the ``mask`` bits of a register; return the result.

        With ``store`` the unit is also told to save the value permanently, as
        its own web page does; without it the value lasts only while the
        registers keep being polled.

        A write response proves nothing on this panel: it acknowledges writes it
        then drops. So the register is read before and after, and a value that
        did not move raises ``PureModbusWriteIgnored``. The panel may also clamp
        what it accepts (a speed of 10 becomes 20), hence "moved", not "equal".
        """
        async with self._lock:
            try:
                return await asyncio.wait_for(
                    self._write_register(register, value, mask, store),
                    timeout=REQUEST_TIMEOUT * 3,
                )
            except asyncio.TimeoutError as err:
                raise PureModbusError(
                    f"Timeout talking Modbus to {self._host}:{self._port}"
                ) from err
            except (OSError, asyncio.IncompleteReadError) as err:
                raise PureModbusError(
                    f"Modbus connection to {self._host}:{self._port} failed: {err}"
                ) from err

    async def _write_register(
        self, register: int, value: int, mask: int, store: bool
    ) -> int:
        reader, writer = await asyncio.open_connection(self._host, self._port)
        try:
            (before,) = await self._read_holding(reader, writer, register - 1, 1)
            wanted = (before & ~mask | value & mask) & 0xFFFF
            if wanted == before:
                return before
            await self._exchange(
                reader,
                writer,
                struct.pack(">BHH", FUNCTION_WRITE_SINGLE, register - 1, wanted),
            )
            await asyncio.sleep(WRITE_SETTLE_TIME)
            (after,) = await self._read_holding(reader, writer, register - 1, 1)
            if after == before:
                raise PureModbusWriteIgnored(
                    f"The unit ignored the write to register {register}; it refuses "
                    "Modbus writes for 60 s after a change made from its own "
                    "web page or touch panel"
                )
            if store:
                (flags,) = await self._read_holding(
                    reader, writer, REG_REMOTE_CONTROL - 1, 1
                )
                store_bit = (
                    REMOTE_STORE_SETPOINTS
                    if register >= FIRST_COMMAND_REGISTER
                    else REMOTE_STORE_CONFIG
                )
                await self._exchange(
                    reader,
                    writer,
                    struct.pack(
                        ">BHH",
                        FUNCTION_WRITE_SINGLE,
                        REG_REMOTE_CONTROL - 1,
                        (flags | store_bit) & ~REMOTE_DEVICE_RESET,
                    ),
                )
            return after
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass

    async def test_connection(self) -> bool:
        """True when the panel answers a register read. Used to detect Modbus."""
        try:
            await self.read_registers(((REG_SW_VERSION_YEAR_MONTH, 2),))
            return True
        except PureModbusError:
            return False


def _bit(value: int, bit: int) -> bool:
    return bool(value >> bit & 1)


def _signed(value: int) -> int:
    return value - 0x10000 if value & 0x8000 else value


def _temperature(registers: dict[int, int], register: int, failed: bool) -> float | None:
    # A failed probe line reports garbage, so surface it as unknown instead.
    if failed:
        return None
    return _signed(registers[register]) / 10


def _software_version(registers: dict[int, int]) -> str | None:
    """Registers 3-4 hold the build date and patch as BCD: YYMM, DDPP."""
    year_month = registers[REG_SW_VERSION_YEAR_MONTH]
    day_patch = registers[REG_SW_VERSION_DAY_PATCH]
    if not year_month and not day_patch:
        return None
    return f"{year_month >> 8:02x}.{year_month & 0xFF:02x}.{day_patch >> 8:02x}.{day_patch & 0xFF:02x}"


def _fan_speed_is_rpm(config_flags: int) -> bool:
    """Whether registers 87/88 hold RPM rather than a percentage.

    The manual ties it to the fan-alarm type in CONFIG_FLAGS_1, but some panels
    leave that whole register at 0 while still reporting RPM from tacho fans
    (seen on a Pure 250). So only an explicitly non-tacho configuration means %.
    """
    if config_flags == 0:
        return True
    return (config_flags >> 8 & 0b11) == FANS_FAIL_TACH


def _factory_configuration(registers: dict[int, int]) -> dict[str, Any]:
    """The Factory-menu configuration, when the panel publishes it.

    Some panels leave registers 7 and 8 at 0 whatever the unit's real build
    (a Pure 250 does, although it has tacho fans and a bypass), so an all-zero
    register 7 reads as "not reported" rather than as a unit with nothing fitted.
    """
    flags_1 = registers[REG_CONFIG_FLAGS_1]
    if flags_1 == 0:
        return {"configuration_reported": False}
    config: dict[str, Any] = {"configuration_reported": True}
    for name, first_bit, bit_count, states in FACTORY_CONFIG_1:
        config[name] = states.get(flags_1 >> first_bit & (1 << bit_count) - 1)
    config["board_model"] = BOARD_MODELS.get(registers[REG_CONFIG_FLAGS_2] & 0b11)
    return config


def decode_registers(registers: dict[int, int]) -> dict[str, Any]:
    """Translate raw registers into the coordinator's data dictionary."""
    alarms1 = registers[REG_ALARMS_1]
    alarms2 = registers[REG_ALARMS_2]
    status = registers[REG_STATUS_FLAGS]
    parameters = registers[REG_PARAMETER_FLAGS]
    speed_setpoint = registers[REG_SPEED_SETPOINT]
    temp_setpoint = registers[REG_TEMP_SETPOINT]
    balance = registers[REG_SPEED_BALANCE]

    faults = {
        "fault_communication": _bit(alarms1, ALARM1_COMM_X540)
        or _bit(alarms1, ALARM1_COMM_X531),
        "fault_temp_external": _bit(alarms1, ALARM1_TEMP_EXTERNAL),
        "fault_temp_return": _bit(alarms1, ALARM1_TEMP_RETURN),
        "fault_temp_exhaust": _bit(alarms1, ALARM1_TEMP_EXHAUST),
        "fault_temp_inlet": _bit(alarms1, ALARM1_TEMP_INLET),
        "fault_fans": _bit(alarms1, ALARM1_FANS),
        "fault_configuration": _bit(alarms2, ALARM2_CONFIGURATION),
        "fault_anti_frost": _bit(alarms2, ALARM2_ANTI_FROST),
    }
    filter_dirty = _bit(alarms1, ALARM1_FILTERS)

    active_alarms = [key.removeprefix("fault_") for key, on in faults.items() if on]
    if filter_dirty:
        active_alarms.append("filter")

    boost_remaining = registers[REG_BOOST_TIMER]
    if boost_remaining:
        operating_mode = "boost"
    elif speed_setpoint == 0:
        operating_mode = "off"
    elif speed_setpoint == SPEED_SETPOINT_TIMER:
        operating_mode = "schedule"
    elif speed_setpoint == SPEED_SETPOINT_AUTO:
        operating_mode = "auto"
    else:
        operating_mode = "manual"

    return {
        "speed": speed_setpoint,
        "timer_mode": speed_setpoint == SPEED_SETPOINT_TIMER,
        "temp_external": _temperature(
            registers, REG_TEMP_EXTERNAL, faults["fault_temp_external"]
        ),
        "temp_return": _temperature(
            registers, REG_TEMP_RETURN, faults["fault_temp_return"]
        ),
        "temp_exhaust": _temperature(
            registers, REG_TEMP_EXHAUST, faults["fault_temp_exhaust"]
        ),
        "temp_inlet": _temperature(
            registers, REG_TEMP_INLET, faults["fault_temp_inlet"]
        ),
        "temp_setpoint": (
            None if temp_setpoint <= TEMP_SETPOINT_OFF_MAX else temp_setpoint / 10
        ),
        # Any raised bit counts, including ones this integration does not name.
        "alarm_active": bool(alarms1 or alarms2),
        "active_alarms": active_alarms,
        "bypass": _bit(status, STATUS_BYPASS_OPEN),
        "filter_dirty": filter_dirty,
        **faults,
        "anti_frost": _bit(status, STATUS_ANTI_FROST_ACTIVE),
        "fan_supply_speed": registers[REG_FAN_SUPPLY_SPEED],
        "fan_exhaust_speed": registers[REG_FAN_EXHAUST_SPEED],
        "fan_speed_is_rpm": _fan_speed_is_rpm(registers[REG_CONFIG_FLAGS_1]),
        "fan_hours": registers[REG_FAN_HOURS_HIGH] * 65536
        + registers[REG_FAN_HOURS_LOW],
        "filter_max_hours": registers[REG_FILTER_MAX_HOURS] * FILTER_HOURS_STEP,
        # Outside its range the panel does not use the parameter (it reads 0)
        "speed_balance": (
            balance if SPEED_BALANCE_MIN <= balance <= SPEED_BALANCE_MAX else None
        ),
        **_factory_configuration(registers),
        "season": SEASONS.get(parameters & 0b11),
        "bypass_mode": BYPASS_MODES.get(parameters >> 2 & 0b11),
        # CONFIG_FLAGS_1 bits 0-2: the kind of post-heating, 0 = none
        "heater_fitted": bool(registers[REG_CONFIG_FLAGS_1] & 0b111),
        "bypass_mode_selectable": (registers[REG_CONFIG_FLAGS_1] >> 10 & 0b11)
        == BYPASS_UNIVERSAL_ON_OFF,
        "operating_mode": operating_mode,
        "boost_remaining": boost_remaining,
        "sw_version": _software_version(registers),
    }
