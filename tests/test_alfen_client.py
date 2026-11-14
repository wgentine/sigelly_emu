# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""Alfen poller: invalid register blocks keep the last-good sample."""

from __future__ import annotations

import struct
import unittest

from app.alfen.client import AlfenClient
from app.alfen.registers import REGISTER_COUNT, REGISTER_START
from app.state.energy import EnergyStore


def _register_block(*, total_power: float = 1500.0, voltage: float = 230.0) -> list[int]:
    regs = [0] * REGISTER_COUNT

    def put_f32(addr: int, value: float) -> None:
        offset = addr - REGISTER_START
        hi, lo = struct.unpack(">HH", struct.pack(">f", value))
        regs[offset : offset + 2] = [hi, lo]

    put_f32(306, voltage)
    put_f32(344, total_power)
    return regs


class AlfenInvalidBlockTests(unittest.TestCase):
    def test_short_or_invalid_block_keeps_last_good_for_store(self) -> None:
        store = EnergyStore(state_path="/tmp/sigelly-alfen-invalid-block.json", use_alfen_energy=False)
        client = AlfenClient("127.0.0.1", port=1, on_update=store.update_from_alfen)
        client._ensure_connected = lambda: True  # type: ignore[method-assign]
        client._read_holding = lambda: _register_block()  # type: ignore[method-assign]

        good = client.poll_once()
        self.assertTrue(good.raw_ok)
        self.assertAlmostEqual(good.total_act_power, 1500.0, places=1)
        self.assertAlmostEqual(store.state.total_act_power, 1500.0, places=1)
        self.assertEqual(client.poll_count, 1)

        client._read_holding = lambda: [0] * 10  # type: ignore[method-assign]
        short = client.poll_once()
        self.assertFalse(short.raw_ok)
        self.assertAlmostEqual(short.total_act_power, 1500.0, places=1)
        self.assertAlmostEqual(store.state.total_act_power, 1500.0, places=1)
        self.assertEqual(client.poll_count, 1)
        self.assertEqual(client.error_count, 1)

        nan_regs = _register_block()
        off = 344 - REGISTER_START
        hi, lo = struct.unpack(">HH", struct.pack(">f", float("nan")))
        nan_regs[off : off + 2] = [hi, lo]
        client._read_holding = lambda: nan_regs  # type: ignore[method-assign]

        invalid = client.poll_once()
        self.assertFalse(invalid.raw_ok)
        self.assertAlmostEqual(invalid.total_act_power, 1500.0, places=1)
        self.assertAlmostEqual(store.state.total_act_power, 1500.0, places=1)
        self.assertEqual(client.poll_count, 1)
        self.assertEqual(client.error_count, 2)


if __name__ == "__main__":
    unittest.main()
