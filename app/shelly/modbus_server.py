"""Shelly Gen2 EM/EMData Modbus TCP slave (port 502).

Sigenstor polls the physical Pro 3EM over Modbus TCP, not HTTP RPC.
Official Shelly docs list addresses 31000+; on the wire those are input
registers at ``address - 30000``. Floats are IEEE-754 word-swapped (CDAB).

Implemented as a minimal asyncio FC03/FC04 slave — pymodbus 3.14's
datastore is write-once / SimDevice oriented and awkward for live meters.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import threading
import time
from typing import Dict, List, Optional

from app.config import Settings
from app.state.energy import EnergyStore, MeterState

logger = logging.getLogger(__name__)

_FC_READ_HOLDING = 0x03
_FC_READ_INPUT = 0x04
_EX_ILLEGAL_ADDRESS = 0x02


def _float_to_regs(value: float) -> List[int]:
    """Encode float32 as Shelly word-swapped Modbus registers (CDAB)."""
    hi, lo = struct.unpack(">HH", struct.pack(">f", float(value)))
    return [lo, hi]


def _uint32_to_regs(value: int) -> List[int]:
    hi, lo = struct.unpack(">HH", struct.pack(">I", int(value) & 0xFFFFFFFF))
    return [lo, hi]


def _ascii_word_swapped(text: str, n_regs: int) -> List[int]:
    raw = text.encode("ascii", errors="replace")
    padded = (raw + b"\x00" * (n_regs * 2))[: n_regs * 2]
    return [(padded[i + 1] << 8) | padded[i] for i in range(0, len(padded), 2)]


def build_register_map(settings: Settings, state: MeterState) -> Dict[int, int]:
    """Full sparse input-register map (also served for holding reads)."""
    out: Dict[int, int] = {}

    def put_float(addr: int, value: float) -> None:
        lo, hi = _float_to_regs(value)
        out[addr] = lo
        out[addr + 1] = hi

    def put_u32(addr: int, value: int) -> None:
        lo, hi = _uint32_to_regs(value)
        out[addr] = lo
        out[addr + 1] = hi

    # Identity @0 (MAC + model), matching real SPEM packing
    for i, reg in enumerate(
        _ascii_word_swapped(settings.mac_no_colons, 6)
        + _ascii_word_swapped(settings.shelly_model, 8)
    ):
        out[i] = reg

    ts = int(state.last_update_ts or time.time())
    put_u32(1000, ts)
    out[1002] = 0
    out[1003] = 0
    out[1004] = 0
    out[1005] = 0
    out[1006] = 0
    put_float(1007, float(state.n_current or 0.0))
    out[1009] = 0
    out[1010] = 0
    put_float(1011, state.a_current + state.b_current + state.c_current)
    put_float(1013, state.total_act_power)
    put_float(1015, state.total_aprt_power)

    put_float(1020, state.a_voltage)
    put_float(1022, state.a_current)
    put_float(1024, state.a_act_power)
    put_float(1026, state.a_aprt_power)
    put_float(1028, state.a_pf)
    out[1030] = out[1031] = out[1032] = 0
    put_float(1033, state.a_freq)

    put_float(1040, state.b_voltage)
    put_float(1042, state.b_current)
    put_float(1044, state.b_act_power)
    put_float(1046, state.b_aprt_power)
    put_float(1048, state.b_pf)
    out[1050] = out[1051] = out[1052] = 0
    put_float(1053, state.b_freq)

    put_float(1060, state.c_voltage)
    put_float(1062, state.c_current)
    put_float(1064, state.c_act_power)
    put_float(1066, state.c_aprt_power)
    put_float(1068, state.c_pf)
    out[1070] = out[1071] = out[1072] = 0
    put_float(1073, state.c_freq)

    e = state.energy
    put_u32(1160, ts)
    put_float(1162, e.total_act)
    put_float(1164, e.total_act_ret)
    put_float(1170, e.a_total_act_energy)
    put_float(1174, e.a_total_act_ret_energy)
    put_float(1182, e.a_total_act_energy)
    put_float(1184, e.a_total_act_ret_energy)
    put_float(1190, e.b_total_act_energy)
    put_float(1194, e.b_total_act_ret_energy)
    put_float(1202, e.b_total_act_energy)
    put_float(1204, e.b_total_act_ret_energy)
    put_float(1210, e.c_total_act_energy)
    put_float(1214, e.c_total_act_ret_energy)
    put_float(1222, e.c_total_act_energy)
    put_float(1224, e.c_total_act_ret_energy)
    return out


class ShellyModbusServer:
    """Minimal Modbus TCP slave for Shelly EM/EMData registers."""

    def __init__(
        self,
        settings: Settings,
        store: EnergyStore,
        host: str = "0.0.0.0",
        port: int = 502,
        unit_id: int = 1,
    ) -> None:
        self.settings = settings
        self.store = store
        self.host = host
        self.port = port
        self.unit_id = unit_id
        self._thread: Optional[threading.Thread] = None
        self._server: Optional[asyncio.AbstractServer] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop = threading.Event()

    def _regs(self) -> Dict[int, int]:
        return build_register_map(self.settings, self.store.snapshot())

    def _handle_pdu(self, _unit: int, pdu: bytes) -> bytes:
        # Real Shelly answers any unit id (Sigen uses 7); caller echoes it back.
        if len(pdu) < 1:
            return b""
        fc = pdu[0]
        if fc not in (_FC_READ_HOLDING, _FC_READ_INPUT):
            # Real fw returns illegal address for unsupported FCs (e.g. FC01 coils).
            return bytes([fc | 0x80, _EX_ILLEGAL_ADDRESS])
        if len(pdu) < 5:
            return bytes([fc | 0x80, _EX_ILLEGAL_ADDRESS])
        address = (pdu[1] << 8) | pdu[2]
        count = (pdu[3] << 8) | pdu[4]
        if count < 1 or count > 125:
            return bytes([fc | 0x80, _EX_ILLEGAL_ADDRESS])

        # Real device: FC03 (holding) → illegal address; only FC04 serves EM data.
        if fc == _FC_READ_HOLDING:
            return bytes([fc | 0x80, _EX_ILLEGAL_ADDRESS])

        regs = self._regs()
        values: List[int] = []
        for i in range(count):
            values.append(int(regs.get(address + i, 0)) & 0xFFFF)
        payload = bytearray([fc, count * 2])
        for v in values:
            payload.append((v >> 8) & 0xFF)
            payload.append(v & 0xFF)
        return bytes(payload)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        peer_s = peer[0] if peer else "?"
        try:
            while not self._stop.is_set():
                header = await asyncio.wait_for(reader.readexactly(6), timeout=120.0)
                tid = header[0:2]
                length = (header[4] << 8) | header[5]
                if length < 2 or length > 260:
                    break
                body = await reader.readexactly(length)
                unit = body[0]
                pdu = body[1:]
                resp_pdu = self._handle_pdu(unit, pdu)
                if not resp_pdu:
                    continue
                resp = tid + b"\x00\x00" + struct.pack(">H", 1 + len(resp_pdu)) + bytes([unit]) + resp_pdu
                writer.write(resp)
                await writer.drain()
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionResetError):
            pass
        except Exception:  # noqa: BLE001
            logger.exception("modbus client error peer=%s", peer_s)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _run(self) -> None:
        self._server = await asyncio.start_server(self._client, self.host, self.port)
        sockets = ", ".join(str(s.getsockname()) for s in self._server.sockets or [])
        logger.info("Shelly Modbus TCP listening on %s unit=%s", sockets, self.unit_id)
        async with self._server:
            await self._server.serve_forever()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def _target() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                loop.run_until_complete(self._run())
            except Exception:  # noqa: BLE001
                if not self._stop.is_set():
                    logger.exception("Modbus server crashed")
            finally:
                try:
                    loop.close()
                except Exception:  # noqa: BLE001
                    pass

        self._thread = threading.Thread(target=_target, name="shelly-modbus", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._loop and self._server:
            def _close() -> None:
                self._server.close()

            self._loop.call_soon_threadsafe(_close)
