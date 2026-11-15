# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""Alfen poller failure handling without a live Modbus socket."""

from __future__ import annotations

import inspect
import struct
import threading
import unittest
from typing import Any
from unittest.mock import patch

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

from app.alfen.client import AlfenClient
from app.alfen.registers import REGISTER_COUNT, REGISTER_START, AlfenMeasurements


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


class _FakeSocket:
    """Stand-in for pymodbus' TCP client. Records the kwargs the poller sends."""

    def __init__(
        self,
        result: Any = None,
        error: BaseException | None = None,
        *,
        reject_device_id: bool = False,
    ) -> None:
        self.result = result
        self.error = error
        self.reject_device_id = reject_device_id
        self.calls: list[dict[str, Any]] = []
        self.connected = True
        self.closed = False

    def read_holding_registers(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if self.reject_device_id and "device_id" in kwargs:
            raise TypeError("unexpected keyword argument 'device_id'")
        if self.error is not None:
            raise self.error
        return self.result

    def close(self) -> None:
        self.closed = True


class _Registers:
    def __init__(self, registers: list[int] | None, is_error: bool = False, label: str = "ok") -> None:
        self.registers = registers
        self._is_error = is_error
        self.label = label

    def isError(self) -> bool:  # noqa: N802 — pymodbus spelling
        return self._is_error

    def __repr__(self) -> str:
        return self.label


class AlfenReadAndReconnectTests(unittest.TestCase):
    def test_installed_pymodbus_accepts_device_id(self) -> None:
        params = inspect.signature(ModbusTcpClient.read_holding_registers).parameters
        device_id = params["device_id"]
        self.assertEqual(device_id.kind, inspect.Parameter.KEYWORD_ONLY)

    def test_read_passes_register_window_and_falls_back_to_slave(self) -> None:
        client = AlfenClient("10.1.1.1", port=1502, slave_id=7)
        modern = _FakeSocket(result=_Registers([9, 8, 7]))
        client._client = modern  # type: ignore[assignment]
        self.assertEqual(client._read_holding(), [9, 8, 7])
        self.assertEqual(
            modern.calls,
            [{"address": REGISTER_START, "count": REGISTER_COUNT, "device_id": 7}],
        )

        legacy = _FakeSocket(result=_Registers([4, 5]), reject_device_id=True)
        client._client = legacy  # type: ignore[assignment]
        self.assertEqual(client._read_holding(), [4, 5])
        self.assertEqual(legacy.calls[0]["device_id"], 7)
        self.assertEqual(
            legacy.calls[1],
            {"address": REGISTER_START, "count": REGISTER_COUNT, "slave": 7},
        )

    def test_read_errors_are_classified_and_do_not_raise(self) -> None:
        client = AlfenClient("10.1.1.1", slave_id=1)
        cases = (
            (_FakeSocket(error=ModbusException("timeout")), str(ModbusException("timeout"))),
            (_FakeSocket(error=OSError("reset")), "modbus_exception:reset"),
            (_FakeSocket(result=_Registers(None, is_error=True, label="err-result")), "modbus_error:err-result"),
            (_FakeSocket(result=_Registers([])), "empty_registers"),
            (_FakeSocket(result=None), "modbus_error:None"),
        )
        for socket, expected in cases:
            with self.subTest(expected=expected):
                client._client = socket  # type: ignore[assignment]
                self.assertIsNone(client._read_holding())
                self.assertEqual(client.last_error, expected)

        client._client = None
        self.assertIsNone(client._read_holding())

    def test_modbus_exception_keeps_last_good_snapshot(self) -> None:
        client = AlfenClient("10.1.1.1", port=1502, slave_id=1)
        client._ensure_connected = lambda: True  # type: ignore[method-assign]
        good = _FakeSocket(result=_Registers(_register_block()))
        client._client = good  # type: ignore[assignment]
        first = client.poll_once()
        self.assertTrue(first.raw_ok)

        client._client = _FakeSocket(error=ModbusException("timeout"))  # type: ignore[assignment]
        failed = client.poll_once()
        self.assertFalse(failed.raw_ok)
        self.assertAlmostEqual(failed.a_voltage, 230.0, places=2)
        self.assertAlmostEqual(failed.total_act_power, 1500.0, places=1)
        self.assertEqual(failed.errors, [str(ModbusException("timeout"))])
        self.assertEqual(client.poll_count, 1)
        self.assertEqual(client.error_count, 1)
        self.assertFalse(client.connected)
        self.assertIsNone(client._client)

    def test_health_status_stays_readable_during_the_modbus_round_trip(self) -> None:
        client = AlfenClient("10.1.1.1", slave_id=1)
        entered = threading.Event()
        release = threading.Event()

        class Slow:
            def read_holding_registers(self, **kwargs: Any) -> _Registers:
                self.kwargs = kwargs
                entered.set()
                if not release.wait(2.0):
                    raise TimeoutError("register read was not released")
                return _Registers(_register_block())

        slow = Slow()
        client._client = slow  # type: ignore[assignment]
        client._ensure_connected = lambda: True  # type: ignore[method-assign]
        box: dict[str, AlfenMeasurements] = {}

        worker = threading.Thread(
            target=lambda: box.setdefault("result", client.poll_once()),
            daemon=True,
        )
        worker.start()
        self.assertTrue(entered.wait(2.0))

        status: dict[str, Any] = {}
        done = threading.Event()

        def read_status() -> None:
            status["value"] = client.get_status()
            done.set()

        watcher = threading.Thread(target=read_status, daemon=True)
        watcher.start()
        try:
            self.assertTrue(done.wait(1.0), "get_status blocked while a register read was in flight")
            self.assertEqual(status["value"]["poll_count"], 0)
        finally:
            release.set()
            worker.join(2.0)
            watcher.join(1.0)
        self.assertFalse(worker.is_alive())
        self.assertTrue(box["result"].raw_ok)
        self.assertEqual(slow.kwargs["device_id"], 1)
        self.assertEqual(client.poll_count, 1)


class _ScriptedModbus:
    def __init__(self, host: str, port: int, timeout: float, *, ok: bool, on_connect: Any = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.connected = False
        self.closed = False
        self._ok = ok
        self._on_connect = on_connect

    def connect(self) -> bool:
        if self._on_connect is not None:
            self._on_connect(self)
        self.connected = self._ok
        return self._ok

    def close(self) -> None:
        self.closed = True
        self.connected = False


class AlfenConnectTests(unittest.TestCase):
    def test_failed_socket_is_closed_and_replaced_on_retry(self) -> None:
        created: list[_ScriptedModbus] = []

        def factory(host: str, port: int, timeout: float) -> _ScriptedModbus:
            client = _ScriptedModbus(host, port, timeout, ok=len(created) == 1)
            created.append(client)
            return client

        poller = AlfenClient("10.1.1.1", port=1502, connect_timeout=1.5)
        with patch("app.alfen.client.ModbusTcpClient", factory):
            with self.assertLogs("app.alfen.client", level="WARNING"):
                self.assertFalse(poller._ensure_connected())
            self.assertEqual(poller.last_error, "connect_failed:10.1.1.1:1502")
            self.assertFalse(poller.connected)
            self.assertTrue(poller._ensure_connected())

        self.assertEqual(created[0].host, "10.1.1.1")
        self.assertEqual(created[0].port, 1502)
        self.assertEqual(created[0].timeout, 1.5)
        self.assertTrue(created[0].closed)
        self.assertFalse(created[1].closed)
        self.assertIs(poller._client, created[1])
        self.assertTrue(poller.connected)

    def test_stop_during_connect_discards_the_new_socket(self) -> None:
        created: list[_ScriptedModbus] = []

        def factory(host: str, port: int, timeout: float) -> _ScriptedModbus:
            def on_connect(_: _ScriptedModbus) -> None:
                poller._stop.set()

            client = _ScriptedModbus(host, port, timeout, ok=True, on_connect=on_connect)
            created.append(client)
            return client

        poller = AlfenClient("10.1.1.1", port=502)
        with patch("app.alfen.client.ModbusTcpClient", factory):
            self.assertFalse(poller._ensure_connected())
        self.assertTrue(created[0].closed)
        self.assertIsNone(poller._client)
        self.assertFalse(poller.connected)

    def test_live_socket_is_reused(self) -> None:
        poller = AlfenClient("10.1.1.1")
        sentinel = _ScriptedModbus("10.1.1.1", 502, 1.0, ok=True)
        sentinel.connected = True
        poller._client = sentinel  # type: ignore[assignment]
        with patch("app.alfen.client.ModbusTcpClient") as ctor:
            self.assertTrue(poller._ensure_connected())
            ctor.assert_not_called()
        self.assertIs(poller._client, sentinel)
        self.assertFalse(sentinel.closed)


class AlfenBackoffTests(unittest.TestCase):
    def test_backoff_grows_until_a_good_poll_and_then_resets(self) -> None:
        client = AlfenClient("10.0.0.8", poll_interval=2.0)
        samples = [
            AlfenMeasurements(raw_ok=False, errors=["down"]),
            AlfenMeasurements(raw_ok=False, errors=["down"]),
            AlfenMeasurements(raw_ok=True, total_act_power=10.0),
        ]
        client.poll_once = lambda: samples.pop(0)  # type: ignore[method-assign]

        def wait(timeout: float) -> bool:
            waits.append(timeout)
            if len(waits) == 3:
                client._stop.set()
            return client._stop.is_set()

        waits: list[float] = []
        client._stop.wait = wait  # type: ignore[method-assign]
        with patch("app.alfen.client.time.monotonic", lambda: 0.0):
            with self.assertLogs("app.alfen.client", level="WARNING"):
                client._loop()
        self.assertEqual(waits, [3.0, 4.5, 2.0])

    def test_backoff_caps_at_thirty_seconds(self) -> None:
        client = AlfenClient("10.0.0.8", poll_interval=20.0)
        client.poll_once = lambda: AlfenMeasurements(raw_ok=False, errors=["down"])  # type: ignore[method-assign]
        waits: list[float] = []

        def wait(timeout: float) -> bool:
            waits.append(timeout)
            if len(waits) == 2:
                client._stop.set()
            return client._stop.is_set()

        client._stop.wait = wait  # type: ignore[method-assign]
        with patch("app.alfen.client.time.monotonic", lambda: 0.0):
            with self.assertLogs("app.alfen.client", level="WARNING"):
                client._loop()
        self.assertEqual(waits, [30.0, 30.0])

    def test_exception_backs_off_and_long_polls_still_pause(self) -> None:
        client = AlfenClient("10.0.0.8", poll_interval=2.0)
        clock = {"t": 0.0}

        def poll() -> AlfenMeasurements:
            clock["t"] += 0.4
            raise RuntimeError("boom")

        client.poll_once = poll  # type: ignore[method-assign]

        def monotonic() -> float:
            return clock["t"]

        waits: list[float] = []

        def wait(timeout: float) -> bool:
            waits.append(timeout)
            client._stop.set()
            return True

        client._stop.wait = wait  # type: ignore[method-assign]
        with patch("app.alfen.client.time.monotonic", monotonic):
            with self.assertLogs("app.alfen.client", level="ERROR"):
                client._loop()
        self.assertEqual(waits, [2.6])
        self.assertEqual(client.error_count, 1)

        client._stop.clear()
        clock["t"] = 0.0

        def slow_ok() -> AlfenMeasurements:
            clock["t"] += 5.0
            return AlfenMeasurements(raw_ok=True)

        client.poll_once = slow_ok  # type: ignore[method-assign]
        waits.clear()

        def wait_once(timeout: float) -> bool:
            waits.append(timeout)
            client._stop.set()
            return True

        client._stop.wait = wait_once  # type: ignore[method-assign]
        with patch("app.alfen.client.time.monotonic", monotonic):
            client._loop()
        self.assertEqual(waits, [0.1])


if __name__ == "__main__":
    unittest.main()
