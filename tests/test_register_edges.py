# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""Alfen register edges that change which phase Sigenstor is billed for."""

from __future__ import annotations

import struct
import unittest

from app.alfen.registers import REGISTER_COUNT, REGISTER_START, parse_registers


def _f32_regs(value: float) -> list[int]:
    hi, lo = struct.unpack(">HH", struct.pack(">f", value))
    return [hi, lo]


def _put(regs: list[int], addr: int, value: float) -> None:
    offset = addr - REGISTER_START
    regs[offset : offset + 2] = _f32_regs(value)


class RegisterEdgeTests(unittest.TestCase):
    def test_mixed_sign_current_splits_by_magnitude(self) -> None:
        regs = [0xFFFF] * REGISTER_COUNT
        _put(regs, 320, 3.0)
        _put(regs, 322, -1.0)
        _put(regs, 324, 0.0)
        _put(regs, 344, 400.0)

        parsed = parse_registers(regs)
        self.assertAlmostEqual(parsed.a_act_power, 300.0, places=2)
        self.assertAlmostEqual(parsed.b_act_power, 100.0, places=2)
        self.assertAlmostEqual(parsed.c_act_power, 0.0, places=2)
        self.assertAlmostEqual(parsed.frequency, 50.0, places=2)

    def test_explicit_zero_frequency_is_kept(self) -> None:
        regs = [0xFFFF] * REGISTER_COUNT
        _put(regs, 336, 0.0)
        parsed = parse_registers(regs)
        self.assertEqual(parsed.frequency, 0.0)

    def test_one_apparent_phase_does_not_invent_the_others(self) -> None:
        regs = [0xFFFF] * REGISTER_COUNT
        _put(regs, 306, 230.0)
        _put(regs, 308, 230.0)
        _put(regs, 310, 230.0)
        _put(regs, 320, 2.0)
        _put(regs, 322, 2.0)
        _put(regs, 324, 2.0)
        _put(regs, 346, 100.0)
        _put(regs, 352, 500.0)

        parsed = parse_registers(regs)
        self.assertAlmostEqual(parsed.a_aprt_power, 100.0, places=2)
        self.assertAlmostEqual(parsed.b_aprt_power, 0.0, places=2)
        self.assertAlmostEqual(parsed.c_aprt_power, 0.0, places=2)
        self.assertAlmostEqual(parsed.total_aprt_power, 500.0, places=2)


if __name__ == "__main__":
    unittest.main()
