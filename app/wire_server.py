# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""Shelly-wire-accurate HTTP/1.1 (+ WebSocket /rpc) server.

Uvicorn lowercases response headers; real ShellyHTTP/1.0.0 sends Title-Case
in a fixed order. Sigen may fingerprint that. This server formats responses
byte-for-byte like the physical Pro 3EM-3CT63.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from typing import Any, Dict, Tuple

from app.alfen.client import AlfenClient
from app.config import get_settings
from app.debug.trace import RpcTrace
from app.shelly.mdns import ShellyMdns
from app.shelly.modbus_server import ShellyModbusServer
from app.shelly.rpc import ShellyRpcHandler
from app.shelly.responses import build_shelly_http_info
from app.state.energy import EnergyStore

logger = logging.getLogger(__name__)

_REQUEST_RE = re.compile(
    rb"^(?P<method>[A-Z]+)\s+(?P<path>[^\s]+)\s+HTTP/1\.[01]\r\n"
    rb"(?P<headers>(?:[^\r\n]+\r\n)*)\r\n",
    re.DOTALL,
)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def _shelly_http_response(
    body: bytes,
    status: int = 200,
    reason: str = "OK",
    *,
    pragma_no_cache: bool = False,
) -> bytes:
    """Exact header block observed on real SPEM-003CEBEU63 fw 2.0.0.

    Real firmware uses Title-Case names in a fixed order and omits Date.
    ``/shelly`` also includes ``Pragma: no-cache``.
    """
    lines = [
        f"HTTP/1.1 {status} {reason}",
        "Content-Type: application/json",
        f"Content-Length: {len(body)}",
    ]
    if pragma_no_cache:
        lines.append("Pragma: no-cache")
    lines.extend(
        [
            "Server: ShellyHTTP/1.0.0",
            "Connection: close",
            "",
            "",
        ]
    )
    return ("\r\n".join(lines)).encode("ascii") + body


def _parse_query(path: str) -> Tuple[str, Dict[str, str]]:
    if "?" not in path:
        return path, {}
    base, qs = path.split("?", 1)
    params: Dict[str, str] = {}
    for part in qs.split("&"):
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
        else:
            k, v = part, ""
        params[k] = v
    return base, params


def _coerce_params(raw: Dict[str, str]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if "params" in raw:
        try:
            parsed = json.loads(raw["params"])
            if isinstance(parsed, dict):
                out.update(parsed)
        except Exception:  # noqa: BLE001
            pass
    for key, value in raw.items():
        if key in ("method", "params", "batch"):
            continue
        if value.isdigit():
            out[key] = int(value)
        else:
            try:
                out[key] = float(value)
            except Exception:  # noqa: BLE001
                out[key] = value
    return out


class ShellyWireServer:
    def __init__(self) -> None:
        self.settings = get_settings()
        _configure_logging(self.settings.log_level)
        self.store = EnergyStore(
            state_path=self.settings.state_path,
            use_alfen_energy=self.settings.alfen_use_energy,
        )
        self.trace = RpcTrace()
        self.rpc = ShellyRpcHandler(self.settings, self.store, self.trace)
        self.alfen = AlfenClient(
            host=self.settings.alfen_host,
            port=self.settings.alfen_port,
            slave_id=self.settings.alfen_slave_id,
            poll_interval=self.settings.alfen_poll_interval,
            connect_timeout=self.settings.alfen_connect_timeout,
            on_update=self.store.update_from_alfen,
        )
        self.mdns = ShellyMdns(self.settings)
        self.modbus = ShellyModbusServer(
            self.settings,
            self.store,
            port=self.settings.shelly_modbus_port,
            unit_id=self.settings.shelly_modbus_unit_id,
        )

    def start_background(self) -> None:
        self.alfen.start()
        if self.settings.shelly_modbus_enable:
            self.modbus.start()
        threading.Thread(target=self.mdns.start, name="mdns-start", daemon=True).start()
        logger.info(
            "wire server ready advertise=%s:%s device=%s modbus=%s",
            self.settings.advertise_ip,
            self.settings.http_port,
            self.settings.device_hostname,
            f":{self.settings.shelly_modbus_port}" if self.settings.shelly_modbus_enable else "off",
        )

    def stop(self) -> None:
        self.mdns.stop()
        if self.settings.shelly_modbus_enable:
            self.modbus.stop()
        self.alfen.stop()
        self.store.save()

    def _handle_http(
        self,
        method: str,
        path: str,
        headers: Dict[str, str],
        body: bytes,
        peer: str,
    ) -> bytes:
        path_only, query = _parse_query(path)
        try:
            payload, status = self._dispatch(method, path_only, query, body, peer)
            raw = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
            reason = "OK" if status == 200 else "Error"
            if status == 404:
                reason = "Not Found"
            elif status == 400:
                reason = "Bad Request"
            elif status >= 500:
                reason = "Internal Server Error"
            return _shelly_http_response(
                raw,
                status,
                reason,
                pragma_no_cache=(path_only == "/shelly"),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("request failed %s %s", method, path_only)
            err = json.dumps({"error": str(exc)}, separators=(",", ":")).encode()
            return _shelly_http_response(err, 500, "Internal Server Error")

    def _dispatch(
        self,
        method: str,
        path: str,
        query: Dict[str, str],
        body: bytes,
        peer: str,
    ) -> Tuple[Any, int]:
        if method == "GET" and path == "/healthz":
            st = self.alfen.get_status()
            return {
                "ok": True,
                "alfen_connected": st["connected"],
                "alfen_ok": self.store.state.alfen_ok,
                "poll_count": st["poll_count"],
                "advertise_ip": self.settings.advertise_ip,
            }, 200
        if method == "GET" and path == "/shelly":
            self.trace.record(peer, "GET /shelly", "GET", ok=True)
            return build_shelly_http_info(self.settings, self.store.snapshot()), 200
        if method == "GET" and path == "/":
            return {
                "service": "sigelly_emu",
                "device": self.settings.device_hostname,
                "model": self.settings.shelly_model,
            }, 200

        if method == "POST" and path == "/rpc":
            data = json.loads(body.decode() or "null")
            if isinstance(data, list):
                return self.rpc.handle_batch(data, client_ip=peer, transport="POST"), 200
            if isinstance(data, dict):
                return self.rpc.handle_call(data, client_ip=peer, transport="POST"), 200
            return {
                "id": None,
                "src": self.settings.device_hostname,
                "error": {"code": -32600, "message": "Invalid Request"},
            }, 400

        if method == "GET" and path == "/rpc":
            params = _coerce_params(query)
            if "batch" in query:
                batch = json.loads(query["batch"])
                results = []
                for item in batch:
                    if not isinstance(item, dict) or not item.get("method"):
                        continue
                    p = item.get("params") or {}
                    results.append(
                        self.rpc.handle_get_result(
                            str(item["method"]),
                            p if isinstance(p, dict) else {},
                            peer,
                        )
                    )
                return results, 200
            m = query.get("method")
            if not m:
                return {"error": "method required"}, 400
            return self.rpc.handle_get_result(m, params, peer), 200

        if method == "GET" and path.startswith("/rpc/"):
            m = path[len("/rpc/") :]
            return self.rpc.handle_get_result(m, _coerce_params(query), peer), 200

        return {"error": "not found"}, 404

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        peer = "?"
        try:
            addr = writer.get_extra_info("peername")
            if addr:
                peer = str(addr[0])
            data = await asyncio.wait_for(reader.read(65536), timeout=10.0)
            if not data:
                return
            match = _REQUEST_RE.match(data)
            if not match:
                writer.write(_shelly_http_response(b'{"error":"bad request"}', 400, "Bad Request"))
                await writer.drain()
                return

            method = match.group("method").decode()
            path = match.group("path").decode()
            header_blob = match.group("headers").decode("latin-1", errors="replace")
            headers: Dict[str, str] = {}
            for line in header_blob.split("\r\n"):
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            body = data[match.end() :]
            content_length = int(headers.get("content-length", "0") or "0")
            while len(body) < content_length:
                body += await reader.read(content_length - len(body))

            # WebSocket upgrade for /rpc
            if (
                headers.get("upgrade", "").lower() == "websocket"
                and path.split("?", 1)[0] == "/rpc"
            ):
                await self._accept_websocket(reader, writer, headers, peer, body_already=b"")
                return

            resp = self._handle_http(method, path, headers, body, peer)
            logger.info('%s - "%s %s HTTP/1.1" %s', peer, method, path.split("?", 1)[0], resp.split(b" ", 2)[1].decode())
            writer.write(resp)
            await writer.drain()
        except Exception:  # noqa: BLE001
            logger.exception("client handler error peer=%s", peer)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _accept_websocket(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        headers: Dict[str, str],
        peer: str,
        body_already: bytes,
    ) -> None:
        import base64
        import hashlib
        import struct

        key = headers.get("sec-websocket-key", "")
        if not key:
            writer.write(_shelly_http_response(b'{"error":"missing key"}', 400, "Bad Request"))
            await writer.drain()
            return
        accept = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode()
        upgrade = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "Server: ShellyHTTP/1.0.0\r\n"
            "\r\n"
        ).encode()
        writer.write(upgrade)
        await writer.drain()
        logger.info('%s - "WebSocket /rpc" 101', peer)

        buf = bytearray(body_already)
        try:
            while True:
                while len(buf) < 2:
                    chunk = await reader.read(4096)
                    if not chunk:
                        return
                    buf.extend(chunk)
                b0, b1 = buf[0], buf[1]
                opcode = b0 & 0x0F
                masked = (b1 & 0x80) != 0
                length = b1 & 0x7F
                idx = 2
                if length == 126:
                    while len(buf) < 4:
                        buf.extend(await reader.read(4096) or b"")
                        if not buf:
                            return
                    length = struct.unpack("!H", buf[2:4])[0]
                    idx = 4
                elif length == 127:
                    while len(buf) < 10:
                        buf.extend(await reader.read(4096) or b"")
                        if not buf:
                            return
                    length = struct.unpack("!Q", buf[2:10])[0]
                    idx = 10
                mask_len = 4 if masked else 0
                while len(buf) < idx + mask_len + length:
                    chunk = await reader.read(4096)
                    if not chunk:
                        return
                    buf.extend(chunk)
                mask = bytes(buf[idx : idx + mask_len]) if masked else b""
                payload = bytes(buf[idx + mask_len : idx + mask_len + length])
                del buf[: idx + mask_len + length]
                if masked:
                    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
                if opcode == 0x8:  # close
                    return
                if opcode == 0x9:  # ping -> pong
                    await self._ws_send(writer, payload, opcode=0xA)
                    continue
                if opcode != 0x1:  # text only
                    continue
                try:
                    req = json.loads(payload.decode())
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(req, dict):
                    continue
                result = self.rpc.handle_call(req, client_ip=peer, transport="WS")
                await self._ws_send(writer, json.dumps(result, separators=(",", ":")).encode())
        except Exception:  # noqa: BLE001
            logger.exception("websocket error peer=%s", peer)

    async def _ws_send(self, writer: asyncio.StreamWriter, payload: bytes, opcode: int = 0x1) -> None:
        import struct

        header = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            header += bytes([n])
        elif n < 65536:
            header += bytes([126]) + struct.pack("!H", n)
        else:
            header += bytes([127]) + struct.pack("!Q", n)
        writer.write(header + payload)
        await writer.drain()


async def _amain() -> None:
    get_settings.cache_clear()
    srv = ShellyWireServer()
    srv.start_background()
    host = "0.0.0.0"
    port = srv.settings.http_port
    server = await asyncio.start_server(srv.handle_client, host, port)
    sockets = ", ".join(str(s.getsockname()) for s in server.sockets or [])
    logger.info("Shelly wire HTTP listening on %s", sockets)
    try:
        async with server:
            await server.serve_forever()
    finally:
        srv.stop()


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
