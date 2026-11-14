# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""Alfen poller failure handling without a live Modbus socket."""

from __future__ import annotations

import struct
import unittest

from app.alfen.client import AlfenClient
from app.alfen.registers import REGISTER_COUNT, REGISTER_START
from app.state.energy import EnergyStore


def _register_block(
    *,
    total_power: float = 1500.0,
    voltage: float = 230.0,
) -> list[int]:
    regs = [0] * REGISTER_COUNT

    def put_f32(addr: int, value: float) -> None:
        offset = addr - REGISTER_START
        hi, lo = struct.unpack(">HH", struct.pack(">f", value))
        regs[offset : offset + 2] = [hi, lo]

    put_f32(306, voltage)
    put_f32(338, total_power)
    put_f32(344, total_power)
    return regs


class AlfenPollOnceTests(unittest.TestCase):
    def test_connect_failure_before_success_reports_empty_failure(self) -> None:
        seen: list[object] = []
        client = AlfenClient("127.0.0.1", port=1, on_update=seen.append)
        client._ensure_connected = lambda: False  # type: ignore[method-assign]
        client._last_error = "connect_failed:127.0.0.1:1"

        result = client.poll_once()

        self.assertFalse(result.raw_ok)
        self.assertEqual(result.a_voltage, 0.0)
        self.assertEqual(result.total_act_power, 0.0)
        self.assertEqual(result.errors, ["connect_failed:127.0.0.1:1"])
        self.assertEqual(client.poll_count, 0)
        self.assertEqual(client.error_count, 1)
        self.assertEqual(seen, [])

    def test_read_failure_keeps_last_good_measurements(self) -> None:
        client = AlfenClient("127.0.0.1", port=1)
        client._ensure_connected = lambda: True  # type: ignore[method-assign]
        client._read_holding = lambda: _register_block()  # type: ignore[method-assign]

        good = client.poll_once()
        self.assertTrue(good.raw_ok)
        self.assertAlmostEqual(good.a_voltage, 230.0, places=2)
        self.assertAlmostEqual(good.total_act_power, 1500.0, places=1)
        self.assertEqual(client.poll_count, 1)

        client._read_holding = lambda: None  # type: ignore[method-assign]
        client._last_error = "empty_registers"
        failed = client.poll_once()

        self.assertFalse(failed.raw_ok)
        self.assertAlmostEqual(failed.a_voltage, 230.0, places=2)
        self.assertAlmostEqual(failed.a_act_power, 1500.0, places=1)
        self.assertAlmostEqual(failed.total_act_power, 1500.0, places=1)
        self.assertEqual(failed.errors, ["empty_registers"])
        self.assertEqual(client.poll_count, 1)
        self.assertEqual(client.error_count, 1)
        self.assertFalse(client.connected)
        status = client.get_status()
        self.assertFalse(status["connected"])
        self.assertFalse(status["last_ok"])
        self.assertEqual(status["last_error"], "empty_registers")

    def test_on_update_error_does_not_fail_the_poll(self) -> None:
        def boom(_: object) -> None:
            raise RuntimeError("callback failed")

        client = AlfenClient("127.0.0.1", port=1, on_update=boom)
        client._ensure_connected = lambda: True  # type: ignore[method-assign]
        client._read_holding = lambda: _register_block()  # type: ignore[method-assign]

        with self.assertLogs("app.alfen.client", level="ERROR"):
            result = client.poll_once()

        self.assertTrue(result.raw_ok)
        self.assertAlmostEqual(result.total_act_power, 1500.0, places=1)
        self.assertEqual(client.poll_count, 1)
        self.assertEqual(client.error_count, 0)


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