"""Modbus TCP client that polls Alfen Eve Pro socket measurements."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional

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
        # RLock: connect/read helpers and status updates may nest.
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        self._last: AlfenMeasurements = AlfenMeasurements()
        self._connected: bool = False
        self._last_poll_ts: float = 0.0
        self._last_error: Optional[str] = None
        self._poll_count: int = 0
        self._error_count: int = 0

    # --- thread-safe public status accessors (used by HTTP handlers) ---

    @property
    def last(self) -> AlfenMeasurements:
        with self._lock:
            return self._last

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def last_poll_ts(self) -> float:
        with self._lock:
            return self._last_poll_ts

    @property
    def last_error(self) -> Optional[str]:
        with self._lock:
            return self._last_error

    @property
    def poll_count(self) -> int:
        with self._lock:
            return self._poll_count

    @property
    def error_count(self) -> int:
        with self._lock:
            return self._error_count

    def get_status(self) -> Dict[str, Any]:
        """Consistent snapshot for /healthz and /debug."""
        with self._lock:
            return {
                "connected": self._connected,
                "poll_count": self._poll_count,
                "error_count": self._error_count,
                "last_error": self._last_error,
                "last_poll_ts": self._last_poll_ts,
                "last_ok": self._last.raw_ok,
            }

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
            client = self._client
            self._client = None
            self._connected = False
        if client:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

    def _ensure_connected(self) -> bool:
        with self._lock:
            client = self._client
            if client is not None and client.connected:
                self._connected = True
                return True
            self._client = None
            self._connected = False

        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass

        new_client = ModbusTcpClient(
            host=self.host,
            port=self.port,
            timeout=self.connect_timeout,
        )
        ok = bool(new_client.connect())
        with self._lock:
            if self._stop.is_set():
                try:
                    new_client.close()
                except Exception:  # noqa: BLE001
                    pass
                self._connected = False
                return False
            self._client = new_client
            self._connected = ok
            if not ok:
                self._last_error = f"connect_failed:{self.host}:{self.port}"
                logger.warning("Failed to connect to Alfen at %s:%s", self.host, self.port)
            else:
                logger.info("Connected to Alfen Modbus at %s:%s", self.host, self.port)
            return ok

    def _read_holding(self) -> Optional[list]:
        # Do not hold self._lock across the network round-trip — a wedged Modbus
        # socket would otherwise block /healthz and freeze the poller loop bookkeeping.
        with self._lock:
            client = self._client
        if client is None:
            return None
        try:
            try:
                result = client.read_holding_registers(
                    address=REGISTER_START,
                    count=REGISTER_COUNT,
                    device_id=self.slave_id,
                )
            except TypeError:
                result = client.read_holding_registers(
                    address=REGISTER_START,
                    count=REGISTER_COUNT,
                    slave=self.slave_id,
                )
        except ModbusException as exc:
            with self._lock:
                self._last_error = str(exc)
            return None
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._last_error = f"modbus_exception:{exc}"
            return None

        if result is None or (hasattr(result, "isError") and result.isError()):
            with self._lock:
                self._last_error = f"modbus_error:{result!r}"
            return None
        regs = getattr(result, "registers", None)
        if not regs:
            with self._lock:
                self._last_error = "empty_registers"
            return None
        return list(regs)

    def poll_once(self) -> AlfenMeasurements:
        """Perform a single poll; retain last-good values on failure."""
        if not self._ensure_connected():
            with self._lock:
                self._error_count += 1
                failed = AlfenMeasurements(
                    raw_ok=False,
                    errors=[self._last_error or "not_connected"],
                )
                if self._last.raw_ok or self._poll_count > 0:
                    failed = self._merge_keep_last_locked(failed)
                self._last = failed
                return failed

        regs = self._read_holding()
        if regs is None:
            with self._lock:
                self._error_count += 1
                self._connected = False
            self._close()
            with self._lock:
                failed = AlfenMeasurements(
                    raw_ok=False,
                    errors=[self._last_error or "read_failed"],
                )
                if self._last.raw_ok or self._poll_count > 0:
                    failed = self._merge_keep_last_locked(failed)
                self._last = failed
                return failed

        parsed = parse_registers(regs)
        with self._lock:
            self._last = parsed
            self._last_poll_ts = time.time()
            self._poll_count += 1
            self._last_error = (
                None if parsed.raw_ok else (parsed.errors[0] if parsed.errors else "parse_error")
            )
        if self.on_update:
            try:
                self.on_update(parsed)
            except Exception:  # noqa: BLE001
                logger.exception("on_update callback failed")
        return parsed

    def _merge_keep_last_locked(self, failed: AlfenMeasurements) -> AlfenMeasurements:
        """Keep previous numeric snapshot but mark raw_ok False with errors.

        Caller must hold self._lock.
        """
        prev = self._last
        return AlfenMeasurements(
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
                with self._lock:
                    self._error_count += 1
                backoff = min(backoff * 1.5, 30.0)
                logger.exception("Alfen poll exception")
            elapsed = time.monotonic() - started
            self._stop.wait(max(0.1, backoff - elapsed))
