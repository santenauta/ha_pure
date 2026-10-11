"""Fixtures: a fake Modbus panel and the unit's web pages."""
from __future__ import annotations

import asyncio
import struct
from collections.abc import AsyncIterator

import pytest

# Registers captured from a real Pure 250 (unit off, dirty-filter alarm raised).
LIVE_REGISTERS: dict[int, int] = {
    1: 8193, 2: 1292, 3: 0x2501, 4: 0x0900, 5: 17, 14: 100, 20: 34,
    24: 4, 25: 4, 26: 4, 27: 4,
    51: 0, 52: 260, 53: 0, 54: 0,
    81: 297, 82: 293, 83: 289, 84: 295, 85: 200, 89: 50, 90: 16,
}

WEB_PAGES: dict[str, str] = {
    "/ifspeed_sp.html": "<html><body><h2>Off      </h2></body></html>",
    "/iftemp_e.html": "<h3>Te 29.7</h3>",
    "/iftemp_r.html": "<h3>Tr 29.3</h3>",
    "/iftemp_x.html": "<h3>Tx 28.9</h3>",
    "/iftemp_i.html": "<h3>Ti 29.5</h3>",
    "/iftemp_sp.html": "<h2>26.0</h2>",
    "/ifalarms.html": '<h2 style="color:red">DirtyFilters  </h2>',
    "/if7834.html": '<img src="img7834_alarms_on.gif">',
    "/if5523.html": '<img src="img5523_bypass_off.gif">',
}


class FakePanel:
    """A tiny Modbus TCP server: reads, and writes the way the real panel does.

    Like the real one it acknowledges every write, silently drops them while
    ``locked_out`` (the 60 s after a web-page change), raises speeds below
    20 % to 20 %, and clears the "store" bits of register 5 by itself; which
    ones were asked for is kept in ``stored``. It keeps one set-point per
    season: register 52 shows the one of the season the unit is on.
    """

    def __init__(self) -> None:
        self.registers: dict[int, int] = dict(LIVE_REGISTERS)
        self.setpoints: dict[int, int] = {
            season: self.registers[52] for season in (0, 1, 2)
        }
        self.requests = 0
        self.locked_out = False
        self.stored: list[int] = []
        self.port = 0
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                header = await reader.readexactly(7)
                transaction, _, length, unit = struct.unpack(">HHHB", header)
                function, address, count = struct.unpack(
                    ">BHH", await reader.readexactly(length - 1)
                )
                self.requests += 1
                if function == 6:
                    value = count
                    if address + 1 == 51 and 0 < value < 20:
                        value = 20
                    if address + 1 == 5:
                        assert not value & 1 << 13, "device reset bit must never be set"
                        if not self.locked_out and value >> 14:
                            self.stored.append(value >> 14)
                        value &= 0x3FFF
                    if not self.locked_out:
                        if address + 1 == 20 and (value ^ self.registers[20]) & 0b11:
                            self.setpoints[self.registers[20] & 0b11] = self.registers[52]
                            self.registers[52] = self.setpoints[value & 0b11]
                        self.registers[address + 1] = value
                    body = struct.pack(">BHH", function, address, count)
                else:
                    values = [
                        self.registers.get(address + 1 + i, 0) for i in range(count)
                    ]
                    body = struct.pack(f">BB{count}H", function, count * 2, *values)
                writer.write(
                    struct.pack(">HHHB", transaction, 0, len(body) + 1, unit) + body
                )
                await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()


@pytest.fixture
async def panel(socket_enabled) -> AsyncIterator[FakePanel]:
    # Sockets first: newer test harnesses block them until socket_enabled runs
    fake = FakePanel()
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load custom_components/pure."""
    return
