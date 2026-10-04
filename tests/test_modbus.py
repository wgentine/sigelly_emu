# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""Shelly Modbus register map and PDU handling (no live TCP required)."""

from __future__ import annotations

import struct
import unittest

from app.config import Settings
from app.shelly.modbus_server import (
    ShellyModbusServer,
    _float_to_regs,
    build_register_map,
)
from app.state.energy import EnergyStore, MeterState


def _decode_shelly_float(reg_map: dict[int, int], addr: int) -> float:
    """Decode CDAB word-swapped float32 at ``addr`` / ``addr+1``."""
    lo = reg_map[addr]
    hi = reg_map[addr + 1]
    return struct.unpack(">f", struct.pack(">HH", hi, lo))[0]


def _test_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"alfen_host": "127.0.0.1"}
    base.update(overrides)
    return Settings(**base)


class ModbusEncodingTests(unittest.TestCase):
    def test_float_cdab_roundtrip(self) -> None:
        hi, lo = struct.unpack(">HH", struct.pack(">f", 228.3))
        self.assertEqual(_float_to_regs(228.3), [lo, hi])

    def test_phase_a_voltage_at_1020(self) -> None:
        state = MeterState(a_voltage=228.3, last_update_ts=1_700_000_000.0)
        reg_map = build_register_map(_test_settings(), state)
        self.assertAlmostEqual(_decode_shelly_float(reg_map, 1020), 228.3, places=1)

    def test_total_act_power_at_1013(self) -> None:
        state = MeterState(total_act_power=1387.1, last_update_ts=1_700_000_000.0)
        reg_map = build_register_map(_test_settings(), state)
        self.assertAlmostEqual(_decode_shelly_float(reg_map, 1013), 1387.1, places=1)

    def test_energy_block_present(self) -> None:
        state = MeterState(last_update_ts=1_700_000_000.0)
        state.energy.total_act = 12345.6
        reg_map = build_register_map(_test_settings(), state)
        self.assertAlmostEqual(_decode_shelly_float(reg_map, 1162), 12345.6, places=1)


class ModbusPduTests(unittest.TestCase):
    def setUp(self) -> None:
        store = EnergyStore(state_path="/tmp/sigelly-test-state.json", use_alfen_energy=False)
        store.state.a_voltage = 230.0
        store.state.last_update_ts = 1_700_000_000.0
        self.server = ShellyModbusServer(_test_settings(), store, port=5502)

    def test_fc04_reads_input_registers(self) -> None:
        pdu = bytes([0x04, 0x03, 0xFC, 0x00, 0x02])  # addr 1020, count 2
        resp = self.server._handle_pdu(7, pdu)
        self.assertEqual(resp[0], 0x04)
        self.assertEqual(resp[1], 4)
        lo = (resp[2] << 8) | resp[3]
        hi = (resp[4] << 8) | resp[5]
        value = struct.unpack(">f", struct.pack(">HH", hi, lo))[0]
        self.assertAlmostEqual(value, 230.0, places=1)

    def test_fc03_illegal_address(self) -> None:
        pdu = bytes([0x03, 0x03, 0xFC, 0x00, 0x02])
        resp = self.server._handle_pdu(1, pdu)
        self.assertEqual(resp, bytes([0x83, 0x02]))

    def test_any_unit_id_accepted(self) -> None:
        pdu = bytes([0x04, 0x03, 0xFC, 0x00, 0x02])
        for unit in (1, 7, 255):
            resp = self.server._handle_pdu(unit, pdu)
            self.assertEqual(resp[0], 0x04, msg=f"unit {unit}")


if __name__ == "__main__":
    unittest.main()
