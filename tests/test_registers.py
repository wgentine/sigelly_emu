# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""Unit tests for Alfen register decoding (no live hardware required)."""

from __future__ import annotations

import struct
import unittest

from app.alfen.registers import (
    REGISTER_COUNT,
    REGISTER_START,
    decode_float32,
    decode_float64,
    parse_registers,
)


def _f32_regs(value: float) -> list[int]:
    raw = struct.pack(">f", value)
    hi, lo = struct.unpack(">HH", raw)
    return [hi, lo]


def _f64_regs(value: float) -> list[int]:
    raw = struct.pack(">d", value)
    return list(struct.unpack(">HHHH", raw))


class RegisterDecodeTests(unittest.TestCase):
    def test_float32_roundtrip(self) -> None:
        regs = _f32_regs(230.5)
        self.assertAlmostEqual(decode_float32(regs, 0) or 0.0, 230.5, places=2)

    def test_float64_roundtrip(self) -> None:
        regs = _f64_regs(12345.67)
        self.assertAlmostEqual(decode_float64(regs, 0) or 0.0, 12345.67, places=2)

    def test_nan_regs(self) -> None:
        self.assertIsNone(decode_float32([0xFFFF, 0xFFFF], 0))

    def test_parse_powers(self) -> None:
        regs = [0] * REGISTER_COUNT

        def put_f32(addr: int, value: float) -> None:
            off = addr - REGISTER_START
            regs[off : off + 2] = _f32_regs(value)

        def put_f64(addr: int, value: float) -> None:
            off = addr - REGISTER_START
            regs[off : off + 4] = _f64_regs(value)

        put_f32(306, 230.0)
        put_f32(308, 231.0)
        put_f32(310, 229.0)
        put_f32(320, 10.0)
        put_f32(322, 0.0)
        put_f32(324, 0.0)
        put_f32(338, 2300.0)
        put_f32(340, 0.0)
        put_f32(342, 0.0)
        put_f32(344, 2300.0)
        put_f64(362, 100.5)
        put_f64(374, 100.5)

        m = parse_registers(regs)
        self.assertTrue(m.raw_ok)
        self.assertAlmostEqual(m.a_voltage, 230.0, places=1)
        self.assertAlmostEqual(m.a_act_power, 2300.0, places=1)
        self.assertAlmostEqual(m.total_act_power, 2300.0, places=1)
        self.assertAlmostEqual(m.a_total_act_energy or 0.0, 100.5, places=1)

    def test_synthesize_phase_power_from_total_and_current(self) -> None:
        """Alfen often publishes sum power + L1 current with phase power = 0xFFFF."""
        regs = [0xFFFF] * REGISTER_COUNT

        def put_f32(addr: int, value: float) -> None:
            off = addr - REGISTER_START
            regs[off : off + 2] = _f32_regs(value)

        put_f32(306, 229.0)
        put_f32(308, 229.0)
        put_f32(310, 232.0)
        put_f32(320, 6.0)
        put_f32(322, 0.0)
        put_f32(324, 0.0)
        put_f32(336, 50.1)
        put_f32(344, 1380.0)

        m = parse_registers(regs)
        self.assertTrue(m.raw_ok)
        self.assertAlmostEqual(m.total_act_power, 1380.0, places=1)
        self.assertAlmostEqual(m.a_act_power, 1380.0, places=1)
        self.assertAlmostEqual(m.b_act_power, 0.0, places=1)
        self.assertAlmostEqual(m.c_act_power, 0.0, places=1)
        self.assertGreater(m.a_aprt_power, 0.0)
        self.assertGreater(m.a_pf, 0.5)

    def test_short_read_and_nan_payloads(self) -> None:
        short = parse_registers([0] * 10)
        self.assertFalse(short.raw_ok)
        self.assertIn("short_read", short.errors[0])
        self.assertIsNone(decode_float32([1], 0))
        self.assertIsNone(decode_float64([0xFFFF, 0xFFFF, 0xFFFF, 0xFFFF], 0))
        nan_regs = list(struct.unpack(">HH", struct.pack(">f", float("nan"))))
        self.assertIsNone(decode_float32(nan_regs, 0))

    def test_sum_power_when_total_register_missing(self) -> None:
        regs = [0xFFFF] * REGISTER_COUNT

        def put_f32(addr: int, value: float) -> None:
            off = addr - REGISTER_START
            regs[off : off + 2] = _f32_regs(value)

        put_f32(338, 100.0)
        put_f32(340, 200.0)
        put_f32(342, 50.0)
        parsed = parse_registers(regs)
        self.assertTrue(parsed.raw_ok)
        self.assertAlmostEqual(parsed.total_act_power, 350.0, places=2)

    def test_split_power_across_phases_by_current(self) -> None:
        regs = [0xFFFF] * REGISTER_COUNT

        def put_f32(addr: int, value: float) -> None:
            off = addr - REGISTER_START
            regs[off : off + 2] = _f32_regs(value)

        put_f32(306, 230.0)
        put_f32(308, 230.0)
        put_f32(310, 230.0)
        put_f32(320, 2.0)
        put_f32(322, 1.0)
        put_f32(324, 1.0)
        put_f32(344, 400.0)

        parsed = parse_registers(regs)
        self.assertAlmostEqual(parsed.a_act_power, 200.0, places=2)
        self.assertAlmostEqual(parsed.b_act_power, 100.0, places=2)
        self.assertAlmostEqual(parsed.c_act_power, 100.0, places=2)

    def test_power_factor_clamped_and_apparent_from_voltage_current(self) -> None:
        regs = [0xFFFF] * REGISTER_COUNT

        def put_f32(addr: int, value: float) -> None:
            off = addr - REGISTER_START
            regs[off : off + 2] = _f32_regs(value)

        put_f32(306, 100.0)
        put_f32(308, 100.0)
        put_f32(320, 1.0)
        put_f32(322, 1.0)
        put_f32(338, 250.0)
        put_f32(340, -250.0)

        parsed = parse_registers(regs)
        self.assertAlmostEqual(parsed.a_pf, 1.0, places=3)
        self.assertAlmostEqual(parsed.b_pf, -1.0, places=3)
        self.assertAlmostEqual(parsed.c_pf, 0.0, places=3)
        self.assertAlmostEqual(parsed.a_aprt_power, 100.0, places=2)
        self.assertAlmostEqual(parsed.b_aprt_power, 100.0, places=2)
        self.assertAlmostEqual(parsed.total_aprt_power, 200.0, places=2)
        self.assertAlmostEqual(parsed.total_act_power, 0.0, places=2)


if __name__ == "__main__":
    unittest.main()
