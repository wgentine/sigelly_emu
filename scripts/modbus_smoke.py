#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""Minimal Modbus TCP smoke test (stdlib only) for CI and local checks."""

from __future__ import annotations

import argparse
import socket
import struct
import sys


def _mbap(unit: int, pdu: bytes, transaction: int = 1) -> bytes:
    length = len(pdu) + 1
    return struct.pack(">HHH", transaction, 0, length) + bytes([unit]) + pdu


def modbus_request(
    host: str,
    port: int,
    unit: int,
    fc: int,
    address: int,
    count: int,
    timeout: float = 3.0,
) -> bytes:
    pdu = struct.pack(">BHH", fc, address, count)
    req = _mbap(unit, pdu)
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(req)
        header = _recv_exact(sock, 6, timeout)
        _tid, proto, length = struct.unpack(">HHH", header)
        if proto != 0 or length < 2:
            raise RuntimeError(f"bad MBAP header: {header!r}")
        body = _recv_exact(sock, length, timeout)
    return body


def _recv_exact(sock: socket.socket, n: int, timeout: float) -> bytes:
    sock.settimeout(timeout)
    chunks: list[bytes] = []
    got = 0
    while got < n:
        part = sock.recv(n - got)
        if not part:
            raise RuntimeError("connection closed before full response")
        chunks.append(part)
        got += len(part)
    return b"".join(chunks)


def main() -> int:
    parser = argparse.ArgumentParser(description="Shelly Modbus FC04 smoke test")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument("--unit", type=int, default=7, help="Sigen often uses unit 7")
    parser.add_argument("--address", type=int, default=1020, help="Phase A voltage (doc 31020)")
    args = parser.parse_args()

    # FC04 read input registers @1020 (2 regs = float32)
    body = modbus_request(args.host, args.port, args.unit, 0x04, args.address, 2)
    fc = body[1]
    if fc & 0x80:
        raise SystemExit(f"Modbus exception fc=0x{fc:02x} code={body[2]}")
    if fc != 0x04:
        raise SystemExit(f"unexpected function code 0x{fc:02x}")
    byte_count = body[2]
    if byte_count != 4:
        raise SystemExit(f"expected 4 data bytes, got {byte_count}")
    lo = (body[3] << 8) | body[4]
    hi = (body[5] << 8) | body[6]
    value = struct.unpack(">f", struct.pack(">HH", hi, lo))[0]
    print(f"OK Modbus FC04 unit={args.unit} addr={args.address} float={value:.3f}")

    # FC03 must fail like real Shelly firmware
    ex_body = modbus_request(args.host, args.port, args.unit, 0x03, args.address, 2)
    if ex_body[1] != 0x83 or ex_body[2] != 0x02:
        raise SystemExit(f"expected FC03 illegal address, got {ex_body.hex()}")
    print("OK FC03 returns illegal address (matches real device)")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except OSError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
