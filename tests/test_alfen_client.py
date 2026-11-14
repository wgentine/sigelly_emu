# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""Alfen poller failure handling without a live Modbus socket."""

from __future__ import annotations

import struct
import unittest

from app.alfen.client import AlfenClient
from app.alfen.registers import REGISTER_COUNT, REGISTER_START


def _register_block() -> list[int]:
    regs = [0] * REGISTER_COUNT

    def put_f32(addr: int, value: float) -> None:
        offset = addr - REGISTER_START
        hi, lo = struct.unpack(">HH", struct.pack(">f", value))
        regs[offset : offset + 2] = [hi, lo]

    put_f32(306, 230.0)
    put_f32(338, 1500.0)
    put_f32(344, 1500.0)
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


if __name__ == "__main__":
    unittest.main()
