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

    def test_phase_powers_export_and_identity(self) -> None:
        state = MeterState(
            a_act_power=100.0,
            b_act_power=-50.0,
            c_act_power=25.0,
            b_voltage=231.2,
            c_voltage=229.4,
            last_update_ts=1_700_000_000.0,
        )
        state.energy.total_act_ret = 55.25
        state.energy.b_total_act_energy = 12.5
        reg_map = build_register_map(
            _test_settings(shelly_mac="34:94:54:11:22:33"),
            state,
        )
        # MAC "3494..." is stored little-endian within each register: '4','3' then '4','9'.
        self.assertEqual(reg_map[0], (ord("4") << 8) | ord("3"))
        self.assertEqual(reg_map[1], (ord("4") << 8) | ord("9"))
        self.assertAlmostEqual(_decode_shelly_float(reg_map, 1024), 100.0, places=1)
        self.assertAlmostEqual(_decode_shelly_float(reg_map, 1044), -50.0, places=1)
        self.assertAlmostEqual(_decode_shelly_float(reg_map, 1064), 25.0, places=1)
        self.assertAlmostEqual(_decode_shelly_float(reg_map, 1040), 231.2, places=1)
        self.assertAlmostEqual(_decode_shelly_float(reg_map, 1060), 229.4, places=1)
        self.assertAlmostEqual(_decode_shelly_float(reg_map, 1164), 55.25, places=2)
        self.assertAlmostEqual(_decode_shelly_float(reg_map, 1190), 12.5, places=1)

    def test_rejected_requests_and_gaps(self) -> None:
        self.assertEqual(self.server._handle_pdu(1, b""), b"")
        self.assertEqual(self.server._handle_pdu(1, bytes([0x04, 0x00])), bytes([0x84, 0x02]))
        self.assertEqual(
            self.server._handle_pdu(1, bytes([0x01, 0x00, 0x00, 0x00, 0x01])),
            bytes([0x81, 0x02]),
        )
        for count in (0, 126):
            pdu = bytes([0x04, 0x00, 0x00, (count >> 8) & 0xFF, count & 0xFF])
            self.assertEqual(self.server._handle_pdu(1, pdu), bytes([0x84, 0x02]))
        # Address 100 is outside the identity block and the EM block.
        gap = self.server._handle_pdu(1, bytes([0x04, 0x00, 0x64, 0x00, 0x01]))
        self.assertEqual(gap, bytes([0x04, 0x02, 0x00, 0x00]))


if __name__ == "__main__":
    unittest.main()
