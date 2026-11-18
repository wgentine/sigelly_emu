# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""Shelly Modbus register map and PDU handling (no live TCP required)."""

from __future__ import annotations

import asyncio
import socket
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path

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

    def test_mbap_echoes_transaction_and_unit_and_rejects_bad_lengths(self) -> None:
        asyncio.run(self._mbap_session())

    async def _mbap_session(self) -> None:
        left, right = socket.socketpair()
        left.setblocking(False)
        right.setblocking(False)
        client_reader, client_writer = await asyncio.open_connection(sock=left)
        server_reader, server_writer = await asyncio.open_connection(sock=right)
        task = asyncio.create_task(self.server._client(server_reader, server_writer))
        try:
            request = bytes([0x12, 0x34, 0x99, 0x99, 0x00, 0x06, 0x07, 0x04, 0x03, 0xFC, 0x00, 0x02])
            await asyncio.sleep(0)
            client_writer.write(request[:3])
            await client_writer.drain()
            await asyncio.sleep(0)
            client_writer.write(request[3:])
            await client_writer.drain()

            header = await asyncio.wait_for(client_reader.readexactly(6), timeout=2)
            length = (header[4] << 8) | header[5]
            body = await asyncio.wait_for(client_reader.readexactly(length), timeout=2)
            self.assertEqual(header[0:4], b"\x12\x34\x00\x00")
            self.assertEqual(body[0], 7)
            self.assertEqual(body[1], 0x04)
            self.assertEqual(body[2], 4)
            lo = (body[3] << 8) | body[4]
            hi = (body[5] << 8) | body[6]
            value = struct.unpack(">f", struct.pack(">HH", hi, lo))[0]
            self.assertAlmostEqual(value, 230.0, places=1)

            # A second request on the same connection still gets its own transaction id.
            client_writer.write(bytes([0x00, 0x09, 0x00, 0x00, 0x00, 0x06, 0x07, 0x03, 0x03, 0xFC, 0x00, 0x02]))
            await client_writer.drain()
            exc_header = await asyncio.wait_for(client_reader.readexactly(6), timeout=2)
            exc_len = (exc_header[4] << 8) | exc_header[5]
            exc_body = await asyncio.wait_for(client_reader.readexactly(exc_len), timeout=2)
            self.assertEqual(exc_header[0:4], b"\x00\x09\x00\x00")
            self.assertEqual(exc_body, bytes([0x07, 0x83, 0x02]))

            client_writer.close()
            await client_writer.wait_closed()
            await asyncio.wait_for(task, timeout=2)
        finally:
            if not task.done():
                task.cancel()
            client_writer.close()
            server_writer.close()

    def test_mbap_length_out_of_range_closes_without_a_response(self) -> None:
        asyncio.run(self._mbap_bad_length(b"\x00\x01\x00\x00\x01\x05"))
        asyncio.run(self._mbap_bad_length(b"\x00\x01\x00\x00\x00\x01"))

    async def _mbap_bad_length(self, header: bytes) -> None:
        left, right = socket.socketpair()
        left.setblocking(False)
        right.setblocking(False)
        client_reader, client_writer = await asyncio.open_connection(sock=left)
        server_reader, server_writer = await asyncio.open_connection(sock=right)
        task = asyncio.create_task(self.server._client(server_reader, server_writer))
        try:
            client_writer.write(header)
            await client_writer.drain()
            data = await asyncio.wait_for(client_reader.read(64), timeout=2)
            self.assertEqual(data, b"")
            await asyncio.wait_for(task, timeout=2)
        finally:
            if not task.done():
                task.cancel()
            client_writer.close()
            server_writer.close()


class ModbusLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = EnergyStore(
            state_path=str(Path(self._tmp.name) / "state.json"),
            use_alfen_energy=False,
        )
        self.store.state.a_voltage = 230.0
        self.store.state.last_update_ts = 1_700_000_000.0
        self.server = ShellyModbusServer(
            _test_settings(),
            self.store,
            host="127.0.0.1",
            port=0,
        )

    def tearDown(self) -> None:
        thread = self.server._thread
        if thread is not None and thread.is_alive():
            self.server.stop()
            thread.join(timeout=2)
        self._tmp.cleanup()

    def _wait_port(self) -> int:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            listener = self.server._server
            sockets = list(listener.sockets or []) if listener is not None else []
            if sockets:
                return int(sockets[0].getsockname()[1])
            thread = self.server._thread
            if thread is not None and not thread.is_alive():
                self.fail("modbus thread exited before it listened")
            time.sleep(0.01)
        self.fail("modbus server did not listen")

    def _read_fc04(self, port: int) -> bytes:
        request = bytes([0x12, 0x34, 0x00, 0x00, 0x00, 0x06, 0x07, 0x04, 0x03, 0xFC, 0x00, 0x02])
        with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
            sock.settimeout(2)
            sock.sendall(request)
            buf = b""
            while len(buf) < 13:
                chunk = sock.recv(13 - len(buf))
                if not chunk:
                    break
                buf += chunk
            return buf

    def test_start_serves_input_registers_and_stop_releases_the_port(self) -> None:
        thread_errors: list[BaseException] = []
        previous_hook = threading.excepthook

        def _hook(args: threading.ExceptHookArgs) -> None:
            if args.exc_value is not None:
                thread_errors.append(args.exc_value)

        threading.excepthook = _hook
        try:
            self.server.start()
            thread = self.server._thread
            self.assertIsNotNone(thread)
            port = self._wait_port()
            self.server.start()
            self.assertIs(self.server._thread, thread)

            body = self._read_fc04(port)
            self.assertEqual(body[0:3], b"\x12\x34\x00")
            self.assertEqual(body[6], 7)
            self.assertEqual(body[7], 0x04)
            self.assertEqual(body[8], 4)
            lo = (body[9] << 8) | body[10]
            hi = (body[11] << 8) | body[12]
            value = struct.unpack(">f", struct.pack(">HH", hi, lo))[0]
            self.assertAlmostEqual(value, 230.0, places=1)

            assert thread is not None
            self.server.stop()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            with self.assertRaises(ConnectionRefusedError):
                socket.create_connection(("127.0.0.1", port), timeout=1)

            self.server.start()
            restarted = self.server._thread
            self.assertIsNot(restarted, thread)
            restarted_port = self._wait_port()
            again = self._read_fc04(restarted_port)
            self.assertEqual(again[7], 0x04)
            assert restarted is not None
            self.server.stop()
            restarted.join(timeout=2)
            self.assertFalse(restarted.is_alive())
        finally:
            threading.excepthook = previous_hook
        self.assertEqual(thread_errors, [])

    def test_listener_crash_is_logged_unless_shutdown_already_started(self) -> None:
        async def crash() -> None:
            raise RuntimeError("bind failed")

        self.server._run = crash  # type: ignore[method-assign]
        with self.assertLogs("app.shelly.modbus_server", level="ERROR") as logs:
            self.server.start()
            crashed = self.server._thread
            self.assertIsNotNone(crashed)
            assert crashed is not None
            crashed.join(timeout=2)
        self.assertFalse(crashed.is_alive())
        self.assertTrue(any("Modbus server crashed" in line for line in logs.output))
        self.assertTrue(any("bind failed" in line for line in logs.output))

        async def crash_during_stop() -> None:
            self.server._stop.set()
            raise RuntimeError("closed")

        self.server._run = crash_during_stop  # type: ignore[method-assign]
        with self.assertNoLogs("app.shelly.modbus_server", level="ERROR"):
            self.server.start()
            quiet = self.server._thread
            self.assertIsNotNone(quiet)
            assert quiet is not None
            quiet.join(timeout=2)
        self.assertFalse(quiet.is_alive())


if __name__ == "__main__":
    unittest.main()
