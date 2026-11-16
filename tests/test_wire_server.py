# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""Shelly-accurate HTTP response formatting and wire dispatch."""

from __future__ import annotations

import asyncio
import json
import socket
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.wire_server import ShellyWireServer, _shelly_http_response


class WireServerResponseTests(unittest.TestCase):
    def test_title_case_headers_and_no_date(self) -> None:
        body = b'{"ok":true}'
        raw = _shelly_http_response(body)
        text = raw.decode("ascii")
        self.assertIn("Server: ShellyHTTP/1.0.0", text)
        self.assertIn("Connection: close", text)
        self.assertIn("Content-Type: application/json", text)
        self.assertNotIn("Date:", text)
        self.assertTrue(text.endswith('{"ok":true}'))

    def test_pragma_on_shelly_route(self) -> None:
        body = b"{}"
        raw = _shelly_http_response(body, pragma_no_cache=True)
        self.assertIn("Pragma: no-cache", raw.decode("ascii"))


def _settings(state_path: str) -> Settings:
    return Settings(
        alfen_host="127.0.0.1",
        state_path=state_path,
        shelly_advertise_ip="192.168.9.9",
        shelly_device_id="aabbccddeeff",
        shelly_mac="aa:bb:cc:dd:ee:ff",
        mdns_enable=False,
        shelly_modbus_enable=False,
    )


class _WireHarness(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        settings = _settings(str(Path(self._tmp.name) / "state.json"))
        with (
            patch("app.wire_server.get_settings", return_value=settings),
            patch("app.wire_server._configure_logging"),
        ):
            self.srv = ShellyWireServer()

    def tearDown(self) -> None:
        self._tmp.cleanup()


class WireDispatchTests(_WireHarness):
    def test_health_root_shelly_and_unknown_routes(self) -> None:
        health = self.srv._handle_http("GET", "/healthz", {}, b"", "10.0.0.8")
        self.assertIn(b"HTTP/1.1 200 OK", health)
        self.assertNotIn(b"Pragma:", health)
        self.assertNotIn(b"Date:", health)
        payload = json.loads(health.split(b"\r\n\r\n", 1)[1])
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["alfen_connected"])
        self.assertFalse(payload["alfen_ok"])
        self.assertEqual(payload["poll_count"], 0)
        self.assertEqual(payload["advertise_ip"], "192.168.9.9")

        root = self.srv._handle_http("GET", "/", {}, b"", "10.0.0.8")
        root_body = json.loads(root.split(b"\r\n\r\n", 1)[1])
        self.assertEqual(root_body["device"], "shellypro3em63-aabbccddeeff")
        self.assertEqual(root_body["model"], "SPEM-003CEBEU63")

        shelly = self.srv._handle_http("GET", "/shelly", {}, b"", "10.0.0.8")
        self.assertIn(b"Pragma: no-cache", shelly)
        info = json.loads(shelly.split(b"\r\n\r\n", 1)[1])
        self.assertEqual(info["id"], "shellypro3em63-aabbccddeeff")
        self.assertEqual(info["mac"], "AABBCCDDEEFF")
        self.assertFalse(info["auth_en"])

        missing = self.srv._handle_http("GET", "/nope", {}, b"", "10.0.0.8")
        self.assertIn(b"HTTP/1.1 404 Not Found", missing)
        self.assertEqual(json.loads(missing.split(b"\r\n\r\n", 1)[1]), {"error": "not found"})

    def test_post_rpc_object_batch_and_rejected_bodies(self) -> None:
        ok = self.srv._handle_http(
            "POST",
            "/rpc",
            {},
            b'{"id":1,"src":"sigen","method":"EM.GetConfig"}',
            "10.0.0.8",
        )
        self.assertIn(b"HTTP/1.1 200 OK", ok)
        body = json.loads(ok.split(b"\r\n\r\n", 1)[1])
        self.assertEqual(body["id"], 1)
        self.assertEqual(body["dst"], "sigen")
        self.assertEqual(body["src"], "shellypro3em63-aabbccddeeff")
        self.assertEqual(body["result"]["ct_type"], "3x63A")

        batch = self.srv._handle_http(
            "POST",
            "/rpc",
            {},
            b'[{"id":1,"method":"EM.GetConfig"},{"id":2}]',
            "10.0.0.8",
        )
        items = json.loads(batch.split(b"\r\n\r\n", 1)[1])
        self.assertEqual(items[0]["id"], 1)
        self.assertIn("result", items[0])
        self.assertEqual(items[1]["error"]["code"], -32600)

        for raw in (b"", b"null", b"42", b'"str"'):
            with self.subTest(raw=raw):
                rejected = self.srv._handle_http("POST", "/rpc", {}, raw, "10.0.0.8")
                self.assertIn(b"HTTP/1.1 400 Bad Request", rejected)
                error = json.loads(rejected.split(b"\r\n\r\n", 1)[1])
                self.assertEqual(error["error"]["code"], -32600)

        with self.assertLogs("app.wire_server", level="ERROR"):
            broken = self.srv._handle_http("POST", "/rpc", {}, b"{", "10.0.0.8")
        self.assertIn(b"HTTP/1.1 500 Internal Server Error", broken)
        self.assertIn(b"error", broken.split(b"\r\n\r\n", 1)[1])

    def test_get_rpc_query_batch_and_method_path(self) -> None:
        missing = self.srv._handle_http("GET", "/rpc", {}, b"", "10.0.0.8")
        self.assertIn(b"HTTP/1.1 400 Bad Request", missing)
        self.assertEqual(json.loads(missing.split(b"\r\n\r\n", 1)[1]), {"error": "method required"})

        components = self.srv._handle_http(
            "GET",
            "/rpc/Shelly.GetComponents?include=config",
            {},
            b"",
            "10.0.0.8",
        )
        payload = json.loads(components.split(b"\r\n\r\n", 1)[1])
        self.assertIn("config", payload["components"][0])
        self.assertNotIn("status", payload["components"][0])
        self.assertNotIn("error", payload)

        batch_json = json.dumps(
            [
                {"method": "EM.GetConfig"},
                {"method": ""},
                "skip-me",
                {"method": "Missing.Thing"},
            ]
        )
        batched = self.srv._handle_http("GET", "/rpc?batch=" + batch_json, {}, b"", "10.0.0.8")
        results = json.loads(batched.split(b"\r\n\r\n", 1)[1])
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["ct_type"], "3x63A")
        self.assertEqual(results[1]["error"]["code"], 404)


def _client_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    mask = b"\x01\x02\x03\x04"
    masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    return bytes(header) + mask + masked


async def _read_server_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    header = await asyncio.wait_for(reader.readexactly(2), timeout=2)
    self_masked = header[1] & 0x80
    if self_masked:
        raise AssertionError("server frame was masked")
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", await reader.readexactly(2))[0]
    elif length == 127:
        length = struct.unpack("!Q", await reader.readexactly(8))[0]
    payload = await reader.readexactly(length) if length else b""
    return header[0] & 0x0F, payload


class WireSocketTests(_WireHarness):
    def test_parser_accepts_http1_split_bodies_and_rejects_http2(self) -> None:
        asyncio.run(self._http_session())

    async def _http_session(self) -> None:
        health = await self._exchange(b"GET /healthz HTTP/1.0\r\nHost: meter\r\n\r\n")
        self.assertIn(b"HTTP/1.1 200 OK", health)
        self.assertIn(b'"ok":true', health)

        body = b'{"id":8,"method":"EM.GetConfig"}'
        head = (
            b"POST /rpc HTTP/1.1\r\nHost: meter\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\n\r\n"
        )
        split = await self._exchange_split(head + body[:5], body[5:])
        self.assertIn(b"HTTP/1.1 200 OK", split)
        self.assertIn(b'"ct_type":"3x63A"', split)
        self.assertIn(b'"id":8', split)

        rejected = await self._exchange(b"GET /rpc HTTP/2.0\r\nHost: meter\r\n\r\n")
        self.assertIn(b"HTTP/1.1 400 Bad Request", rejected)
        self.assertIn(b"bad request", rejected)

    def test_websocket_upgrade_frames_and_missing_key(self) -> None:
        asyncio.run(self._websocket_session())
        asyncio.run(self._websocket_missing_key())

    async def _websocket_session(self) -> None:
        request = (
            b"GET /rpc?x=1 HTTP/1.1\r\n"
            b"Host: meter\r\n"
            b"Upgrade: websocket\r\n"
            b"Connection: Upgrade\r\n"
            b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            b"\r\n"
        )
        left, right = socket.socketpair()
        left.setblocking(False)
        right.setblocking(False)
        client_reader, client_writer = await asyncio.open_connection(sock=left)
        server_reader, server_writer = await asyncio.open_connection(sock=right)
        task = asyncio.create_task(self.srv.handle_client(server_reader, server_writer))
        try:
            client_writer.write(request)
            await client_writer.drain()
            opening = await self._read_until(client_reader, b"\r\n\r\n")
            self.assertIn(b"HTTP/1.1 101 Switching Protocols", opening)
            self.assertIn(b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", opening)
            self.assertIn(b"Server: ShellyHTTP/1.0.0", opening)

            client_writer.write(_client_frame(b"xyz", opcode=0x9))
            await client_writer.drain()
            opcode, payload = await _read_server_frame(client_reader)
            self.assertEqual(opcode, 0xA)
            self.assertEqual(payload, b"xyz")

            client_writer.write(_client_frame(b"\x00\x01", opcode=0x2))
            client_writer.write(_client_frame(b"{"))
            padded = json.dumps(
                {"id": 7, "method": "EM.GetConfig", "src": "sigen", "pad": "x" * 180}
            ).encode()
            self.assertGreaterEqual(len(padded), 126)
            client_writer.write(_client_frame(padded))
            await client_writer.drain()
            opcode, payload = await _read_server_frame(client_reader)
            self.assertEqual(opcode, 0x1)
            decoded = json.loads(payload)
            self.assertEqual(decoded["id"], 7)
            self.assertEqual(decoded["dst"], "sigen")
            self.assertEqual(decoded["result"]["ct_type"], "3x63A")

            client_writer.write(_client_frame(b"", opcode=0x8))
            await client_writer.drain()
            await asyncio.wait_for(task, timeout=2)
        finally:
            if not task.done():
                task.cancel()
            client_writer.close()
            server_writer.close()

    async def _websocket_missing_key(self) -> None:
        request = (
            b"GET /rpc HTTP/1.1\r\n"
            b"Host: meter\r\n"
            b"Upgrade: WebSocket\r\n"
            b"Connection: Upgrade\r\n"
            b"\r\n"
        )
        response = await self._exchange(request)
        self.assertIn(b"HTTP/1.1 400 Bad Request", response)
        self.assertIn(b"missing key", response)

    async def _exchange(self, request: bytes) -> bytes:
        return await self._exchange_split(request, b"")

    async def _exchange_split(self, first: bytes, rest: bytes) -> bytes:
        left, right = socket.socketpair()
        left.setblocking(False)
        right.setblocking(False)
        client_reader, client_writer = await asyncio.open_connection(sock=left)
        server_reader, server_writer = await asyncio.open_connection(sock=right)
        task = asyncio.create_task(self.srv.handle_client(server_reader, server_writer))
        try:
            await asyncio.sleep(0)
            client_writer.write(first)
            await client_writer.drain()
            if rest:
                await asyncio.sleep(0)
                client_writer.write(rest)
                await client_writer.drain()
            data = await asyncio.wait_for(client_reader.read(65536), timeout=2)
            await asyncio.wait_for(task, timeout=2)
            return data
        finally:
            if not task.done():
                task.cancel()
            client_writer.close()
            server_writer.close()

    async def _read_until(self, reader: asyncio.StreamReader, marker: bytes) -> bytes:
        buf = b""
        while marker not in buf:
            chunk = await asyncio.wait_for(reader.read(4096), timeout=2)
            if not chunk:
                break
            buf += chunk
        return buf


if __name__ == "__main__":
    unittest.main()
