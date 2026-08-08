"""Modbus TCP client that polls Alfen Eve Pro socket measurements."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

from app.alfen.registers import (
    REGISTER_COUNT,
    REGISTER_START,
    AlfenMeasurements,
    parse_registers,
)

logger = logging.getLogger(__name__)


class AlfenClient:
    """Background Modbus poller with reconnect and last-good snapshot."""

    def __init__(
        self,
        host: str,
        port: int = 502,
        slave_id: int = 1,
        poll_interval: float = 2.0,
        connect_timeout: float = 3.0,
        on_update: Optional[Callable[[AlfenMeasurements], None]] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.slave_id = slave_id
        self.poll_interval = poll_interval
        self.connect_timeout = connect_timeout
        self.on_update = on_update

        self._client: Optional[ModbusTcpClient] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self.last: AlfenMeasurements = AlfenMeasurements()
        self.connected: bool = False
        self.last_poll_ts: float = 0.0
        self.last_error: Optional[str] = None
        self.poll_count: int = 0
        self.error_count: int = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="alfen-poller", daemon=True)
        self._thread.start()
        logger.info(
            "Alfen poller started host=%s port=%s slave=%s interval=%ss",
            self.host,
            self.port,
            self.slave_id,
            self.poll_interval,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)
            self._thread = None
        self._close()

    def _close(self) -> None:
        with self._lock:
            if self._client:
                try:
                    self._client.close()
                except Exception:  # noqa: BLE001
                    pass
                self._client = None
            self.connected = False

    def _ensure_connected(self) -> bool:
        with self._lock:
            if self._client and self._client.connected:
                self.connected = True
                return True
            if self._client:
                try:
                    self._client.close()
                except Exception:  # noqa: BLE001
                    pass
            self._client = ModbusTcpClient(
                host=self.host,
                port=self.port,
                timeout=self.connect_timeout,
            )
            ok = bool(self._client.connect())
            self.connected = ok
            if not ok:
                self.last_error = f"connect_failed:{self.host}:{self.port}"
                logger.warning("Failed to connect to Alfen at %s:%s", self.host, self.port)
            else:
                logger.info("Connected to Alfen Modbus at %s:%s", self.host, self.port)
            return ok

    def _read_holding(self) -> Optional[list]:
        with self._lock:
            if not self._client:
                return None
            # pymodbus 3.x uses device_id / slave depending on version
            try:
                result = self._client.read_holding_registers(
                    address=REGISTER_START,
                    count=REGISTER_COUNT,
                    device_id=self.slave_id,
                )
            except TypeError:
                result = self._client.read_holding_registers(
                    address=REGISTER_START,
                    count=REGISTER_COUNT,
                    slave=self.slave_id,
                )
            except ModbusException as exc:
                self.last_error = str(exc)
                return None

        if result is None or (hasattr(result, "isError") and result.isError()):
            self.last_error = f"modbus_error:{result!r}"
            return None
        regs = getattr(result, "registers", None)
        if not regs:
            self.last_error = "empty_registers"
            return None
        return list(regs)

    def poll_once(self) -> AlfenMeasurements:
        """Perform a single poll; retain last-good values on failure."""
        if not self._ensure_connected():
            self.error_count += 1
            failed = AlfenMeasurements(
                raw_ok=False,
                errors=[self.last_error or "not_connected"],
            )
            # Preserve last good instantaneous values for Shelly consumers
            if self.last.raw_ok or self.poll_count > 0:
                failed = self._merge_keep_last(failed)
            self.last = failed
            return failed

        regs = self._read_holding()
        if regs is None:
            self.error_count += 1
            self.connected = False
            self._close()
            failed = AlfenMeasurements(
                raw_ok=False,
                errors=[self.last_error or "read_failed"],
            )
            if self.last.raw_ok or self.poll_count > 0:
                failed = self._merge_keep_last(failed)
            self.last = failed
            return failed

        parsed = parse_registers(regs)
        self.last = parsed
        self.last_poll_ts = time.time()
        self.poll_count += 1
        self.last_error = None if parsed.raw_ok else (parsed.errors[0] if parsed.errors else "parse_error")
        if self.on_update:
            try:
                self.on_update(parsed)
            except Exception:  # noqa: BLE001
                logger.exception("on_update callback failed")
        return parsed

    def _merge_keep_last(self, failed: AlfenMeasurements) -> AlfenMeasurements:
        """Keep previous numeric snapshot but mark raw_ok False with errors."""
        prev = self.last
        keep = AlfenMeasurements(
            a_voltage=prev.a_voltage,
            b_voltage=prev.b_voltage,
            c_voltage=prev.c_voltage,
            a_current=prev.a_current,
            b_current=prev.b_current,
            c_current=prev.c_current,
            n_current=prev.n_current,
            a_pf=prev.a_pf,
            b_pf=prev.b_pf,
            c_pf=prev.c_pf,
            frequency=prev.frequency,
            a_act_power=prev.a_act_power,
            b_act_power=prev.b_act_power,
            c_act_power=prev.c_act_power,
            total_act_power=prev.total_act_power,
            a_aprt_power=prev.a_aprt_power,
            b_aprt_power=prev.b_aprt_power,
            c_aprt_power=prev.c_aprt_power,
            total_aprt_power=prev.total_aprt_power,
            a_total_act_energy=prev.a_total_act_energy,
            b_total_act_energy=prev.b_total_act_energy,
            c_total_act_energy=prev.c_total_act_energy,
            total_act=prev.total_act,
            a_total_act_ret_energy=prev.a_total_act_ret_energy,
            b_total_act_ret_energy=prev.b_total_act_ret_energy,
            c_total_act_ret_energy=prev.c_total_act_ret_energy,
            total_act_ret=prev.total_act_ret,
            raw_ok=False,
            errors=failed.errors,
        )
        return keep

    def _loop(self) -> None:
        backoff = self.poll_interval
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                result = self.poll_once()
                if result.raw_ok:
                    backoff = self.poll_interval
                    logger.debug(
                        "Alfen poll ok P=%.1fW (%.1f/%.1f/%.1f)",
                        result.total_act_power,
                        result.a_act_power,
                        result.b_act_power,
                        result.c_act_power,
                    )
                else:
                    backoff = min(backoff * 1.5, 30.0)
                    logger.warning("Alfen poll failed: %s", result.errors)
            except Exception:  # noqa: BLE001
                self.error_count += 1
                backoff = min(backoff * 1.5, 30.0)
                logger.exception("Alfen poll exception")
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.1, backoff - elapsed))
