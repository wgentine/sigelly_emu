# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""WebSocket frame edges and non-finite meter JSON on the wire server."""

from __future__ import annotations

import asyncio
import json
import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.wire_server import ShellyWireServer

_WS_KEY = "dGhlIHNhbXBsZSBub25jZQ=="
_WS_ACCEPT = "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


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


class _Writer:
    def __init__(self) -> None:
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.buf.extend(data)

    async def drain(self) -> None:
        return None


def _masked_frame(payload: bytes, opcode: int = 0x1, length_bits: int | None = None) -> bytes:
    mask = b"\x01\x02\x03\x04"
    masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
    length = len(payload)
    header = bytearray([0x80 | opcode])
    if length_bits == 127 or (length_bits is None and length >= 65536):
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    elif length_bits == 126 or (length_bits is None and length >= 126):
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | length)
    return bytes(header) + mask + masked


def _unmasked_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    return bytes([0x80 | opcode, len(payload)]) + payload


def _server_frames(buf: bytes) -> list[tuple[int, bytes]]:
    frames: list[tuple[int, bytes]] = []
    index = 0
    while index + 2 <= len(buf):
        if not buf[index] & 0x80:
            raise AssertionError("server frame was fragmented")
        if buf[index + 1] & 0x80:
            raise AssertionError("server frame was masked")
        opcode = buf[index] & 0x0F
        length = buf[index + 1] & 0x7F
        index += 2
        if length == 126:
            length = struct.unpack("!H", buf[index : index + 2])[0]
            index += 2
        elif length == 127:
            length = struct.unpack("!Q", buf[index : index + 8])[0]
            index += 8
        frames.append((opcode, bytes(buf[index : index + length])))
        index += length
    if index != len(buf):
        raise AssertionError(f"trailing bytes: {buf[index:]!r}")
    return frames


class WireEdgeTests(unittest.TestCase):
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

    def test_ws_send_uses_16_and_64_bit_lengths(self) -> None:
        writer = _Writer()

        async def send() -> None:
            await self.srv._ws_send(writer, b"x" * 10)
            await self.srv._ws_send(writer, b"y" * 200)
            await self.srv._ws_send(writer, b"z" * 70000)

        asyncio.run(send())
        frames = _server_frames(bytes(writer.buf))
        self.assertEqual([len(payload) for _opcode, payload in frames], [10, 200, 70000])
        self.assertEqual(frames[0][0], 0x1)
        self.assertEqual(frames[1][1], b"y" * 200)
        self.assertEqual(frames[2][1][:1], b"z")

    def test_concatenated_unmasked_and_extended_frames(self) -> None:
        asyncio.run(self._frames())

    async def _frames(self) -> None:
        short = _unmasked_frame(b'{"id":1,"src":"sigen","method":"EM.GetConfig"}')
        extended = _masked_frame(
            b'{"id":2,"src":"sigen","method":"Sys.GetStatus"}',
            length_bits=127,
        )
        ignored = _masked_frame(b"[1,2,3]")
        close = _masked_frame(b"", opcode=0x8)
        reader = asyncio.StreamReader()
        reader.feed_data(short + extended + ignored + close)
        reader.feed_eof()
        writer = _Writer()

        await self.srv._accept_websocket(
            reader,
            writer,
            {"sec-websocket-key": _WS_KEY},
            "10.4.5.6",
            b"",
        )

        head, rest = bytes(writer.buf).split(b"\r\n\r\n", 1)
        text = head.decode("ascii")
        self.assertIn("HTTP/1.1 101 Switching Protocols", text)
        self.assertIn(f"Sec-WebSocket-Accept: {_WS_ACCEPT}", text)
        self.assertIn("Server: ShellyHTTP/1.0.0", text)
        frames = _server_frames(rest)
        self.assertEqual(len(frames), 2)
        first = json.loads(frames[0][1])
        second = json.loads(frames[1][1])
        self.assertEqual(first["id"], 1)
        self.assertEqual(first["dst"], "sigen")
        self.assertEqual(first["result"]["ct_type"], "3x63A")
        self.assertEqual(second["id"], 2)
        self.assertEqual(second["result"]["mac"], "AABBCCDDEEFF")
        summary = self.srv.trace.summary()
        self.assertEqual(summary["methods"]["EM.GetConfig"], 1)
        self.assertEqual(summary["methods"]["Sys.GetStatus"], 1)
        self.assertTrue(all(event["transport"] == "WS" for event in summary["recent"]))
        self.assertTrue(all(event["client_ip"] == "10.4.5.6" for event in summary["recent"]))

    def test_missing_websocket_key_is_rejected(self) -> None:
        async def reject() -> bytes:
            writer = _Writer()
            await self.srv._accept_websocket(
                asyncio.StreamReader(),
                writer,
                {},
                "10.4.5.6",
                b"",
            )
            return bytes(writer.buf)

        raw = asyncio.run(reject())
        self.assertIn(b"HTTP/1.1 400 Bad Request", raw)
        self.assertIn(b"missing key", raw)
        self.assertNotIn(b"101", raw)

    def test_non_finite_meter_value_stays_valid_json(self) -> None:
        self.srv.store.state.a_voltage = float("nan")
        health = self.srv._handle_http("GET", "/healthz", {}, b"", "10.0.0.8")
        self.assertIn(b"HTTP/1.1 200 OK", health)

        with self.assertLogs("app.wire_server", level="ERROR"):
            raw = self.srv._handle_http("GET", "/rpc/EM.GetStatus", {}, b"", "10.0.0.8")
        head, body = raw.split(b"\r\n\r\n", 1)
        text = head.decode("ascii")
        self.assertIn("HTTP/1.1 500 Internal Server Error", text)
        length = int(text.split("Content-Length: ", 1)[1].split("\r\n", 1)[0])
        self.assertEqual(length, len(body))
        payload = json.loads(body)
        self.assertIn("error", payload)
        self.assertNotIn(b"NaN", body)


if __name__ == "__main__":
    unittest.main()
