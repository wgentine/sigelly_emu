# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""Shelly input-register fields Sigenstor polls beyond phase A voltage."""

from __future__ import annotations

import struct
import unittest

from app.config import Settings
from app.shelly.modbus_server import build_register_map
from app.state.energy import MeterState


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "alfen_host": "127.0.0.1",
        "shelly_mac": "34:94:54:11:22:33",
        "shelly_model": "SPEM-003CEBEU63",
    }
    base.update(overrides)
    return Settings(**base)


def _f32(reg_map: dict[int, int], addr: int) -> float:
    lo = reg_map[addr]
    hi = reg_map[addr + 1]
    return struct.unpack(">f", struct.pack(">HH", hi, lo))[0]


def _u32(reg_map: dict[int, int], addr: int) -> int:
    lo = reg_map[addr]
    hi = reg_map[addr + 1]
    return struct.unpack(">I", struct.pack(">HH", hi, lo))[0]


def _ascii(reg_map: dict[int, int], addr: int, n_regs: int) -> str:
    raw = bytearray()
    for index in range(addr, addr + n_regs):
        word = reg_map[index]
        raw.append(word & 0xFF)
        raw.append((word >> 8) & 0xFF)
    return raw.decode("ascii")


class ModbusMapTests(unittest.TestCase):
    def test_timestamp_frequencies_currents_and_duplicate_energy(self) -> None:
        state = MeterState(
            a_freq=49.9,
            b_freq=50.0,
            c_freq=50.1,
            a_current=1.5,
            b_current=-0.5,
            c_current=0.25,
            n_current=None,
            total_aprt_power=1400.5,
            last_update_ts=1_700_000_000.0,
        )
        state.energy.a_total_act_energy = 10.5
        state.energy.a_total_act_ret_energy = 1.25
        state.energy.b_total_act_energy = 2.0
        state.energy.b_total_act_ret_energy = 0.5
        state.energy.c_total_act_energy = 3.0
        state.energy.c_total_act_ret_energy = 0.75
        reg_map = build_register_map(_settings(), state)

        self.assertEqual(_u32(reg_map, 1000), 1_700_000_000)
        self.assertEqual(_u32(reg_map, 1160), 1_700_000_000)
        self.assertAlmostEqual(_f32(reg_map, 1007), 0.0, places=3)
        self.assertAlmostEqual(_f32(reg_map, 1011), 1.25, places=3)
        self.assertAlmostEqual(_f32(reg_map, 1015), 1400.5, places=2)
        self.assertAlmostEqual(_f32(reg_map, 1033), 49.9, places=2)
        self.assertAlmostEqual(_f32(reg_map, 1053), 50.0, places=2)
        self.assertAlmostEqual(_f32(reg_map, 1073), 50.1, places=2)

        for import_addr, export_addr, imported, exported in (
            (1170, 1174, 10.5, 1.25),
            (1182, 1184, 10.5, 1.25),
            (1190, 1194, 2.0, 0.5),
            (1202, 1204, 2.0, 0.5),
            (1210, 1214, 3.0, 0.75),
            (1222, 1224, 3.0, 0.75),
        ):
            self.assertAlmostEqual(_f32(reg_map, import_addr), imported, places=2)
            self.assertAlmostEqual(_f32(reg_map, export_addr), exported, places=2)

        neutral = MeterState(n_current=0.4, last_update_ts=1_700_000_000.0)
        neutral_map = build_register_map(_settings(), neutral)
        self.assertAlmostEqual(_f32(neutral_map, 1007), 0.4, places=3)

    def test_identity_string_is_word_swapped_and_truncated(self) -> None:
        reg_map = build_register_map(
            _settings(shelly_model="SPEM-003CEBEU63EXTRA"),
            MeterState(last_update_ts=1_700_000_000.0),
        )
        self.assertEqual(_ascii(reg_map, 0, 6), "349454112233")
        # Model field is 8 registers (16 bytes). The 15-character model keeps
        # one overflow byte and drops the rest, without spilling past register 13.
        self.assertEqual(_ascii(reg_map, 6, 8), "SPEM-003CEBEU63E")
        self.assertNotIn("X", _ascii(reg_map, 6, 8))
        self.assertNotIn(14, reg_map)

        short = build_register_map(
            _settings(shelly_model="SPEM"),
            MeterState(last_update_ts=1_700_000_000.0),
        )
        self.assertEqual(_ascii(short, 6, 8), "SPEM" + "\x00" * 12)


if __name__ == "__main__":
    unittest.main()
