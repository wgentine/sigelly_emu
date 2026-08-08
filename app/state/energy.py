"""Measurement state store with energy counter persistence."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.alfen.registers import AlfenMeasurements

logger = logging.getLogger(__name__)


@dataclass
class EnergyCounters:
    a_total_act_energy: float = 0.0
    b_total_act_energy: float = 0.0
    c_total_act_energy: float = 0.0
    total_act: float = 0.0
    a_total_act_ret_energy: float = 0.0
    b_total_act_ret_energy: float = 0.0
    c_total_act_ret_energy: float = 0.0
    total_act_ret: float = 0.0
    source: str = "integrated"  # "alfen" | "integrated"


@dataclass
class MeterState:
    a_voltage: float = 0.0
    b_voltage: float = 0.0
    c_voltage: float = 0.0
    a_current: float = 0.0
    b_current: float = 0.0
    c_current: float = 0.0
    n_current: Optional[float] = None
    a_pf: float = 0.0
    b_pf: float = 0.0
    c_pf: float = 0.0
    a_freq: float = 50.0
    b_freq: float = 50.0
    c_freq: float = 50.0
    a_act_power: float = 0.0
    b_act_power: float = 0.0
    c_act_power: float = 0.0
    total_act_power: float = 0.0
    a_aprt_power: float = 0.0
    b_aprt_power: float = 0.0
    c_aprt_power: float = 0.0
    total_aprt_power: float = 0.0
    energy: EnergyCounters = field(default_factory=EnergyCounters)
    alfen_ok: bool = False
    alfen_errors: List[str] = field(default_factory=list)
    last_update_ts: float = 0.0
    start_ts: float = field(default_factory=time.time)


class EnergyStore:
    """Thread-safe meter state with Alfen energy preference + power integration."""

    def __init__(self, state_path: str = "/data/state.json") -> None:
        self.state_path = Path(state_path)
        self._lock = threading.RLock()
        self.state = MeterState()
        self._last_integrate_ts: Optional[float] = None
        self._load()

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            energy = data.get("energy", {})
            with self._lock:
                self.state.energy = EnergyCounters(
                    a_total_act_energy=float(energy.get("a_total_act_energy", 0.0)),
                    b_total_act_energy=float(energy.get("b_total_act_energy", 0.0)),
                    c_total_act_energy=float(energy.get("c_total_act_energy", 0.0)),
                    total_act=float(energy.get("total_act", 0.0)),
                    a_total_act_ret_energy=float(energy.get("a_total_act_ret_energy", 0.0)),
                    b_total_act_ret_energy=float(energy.get("b_total_act_ret_energy", 0.0)),
                    c_total_act_ret_energy=float(energy.get("c_total_act_ret_energy", 0.0)),
                    total_act_ret=float(energy.get("total_act_ret", 0.0)),
                    source=str(energy.get("source", "integrated")),
                )
            logger.info("Loaded energy state from %s", self.state_path)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to load state from %s", self.state_path)

    def save(self) -> None:
        with self._lock:
            payload = {
                "energy": asdict(self.state.energy),
                "saved_at": time.time(),
            }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, self.state_path)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to persist state to %s", self.state_path)

    def update_from_alfen(self, m: AlfenMeasurements) -> None:
        now = time.time()
        with self._lock:
            self.state.a_voltage = m.a_voltage
            self.state.b_voltage = m.b_voltage
            self.state.c_voltage = m.c_voltage
            self.state.a_current = m.a_current
            self.state.b_current = m.b_current
            self.state.c_current = m.c_current
            self.state.n_current = m.n_current
            self.state.a_pf = m.a_pf
            self.state.b_pf = m.b_pf
            self.state.c_pf = m.c_pf
            self.state.a_freq = m.frequency
            self.state.b_freq = m.frequency
            self.state.c_freq = m.frequency
            self.state.a_act_power = m.a_act_power
            self.state.b_act_power = m.b_act_power
            self.state.c_act_power = m.c_act_power
            self.state.total_act_power = m.total_act_power
            self.state.a_aprt_power = m.a_aprt_power
            self.state.b_aprt_power = m.b_aprt_power
            self.state.c_aprt_power = m.c_aprt_power
            self.state.total_aprt_power = m.total_aprt_power
            self.state.alfen_ok = m.raw_ok
            self.state.alfen_errors = list(m.errors)
            self.state.last_update_ts = now

            if self._use_alfen_energy(m):
                self.state.energy = EnergyCounters(
                    a_total_act_energy=m.a_total_act_energy or 0.0,
                    b_total_act_energy=m.b_total_act_energy or 0.0,
                    c_total_act_energy=m.c_total_act_energy or 0.0,
                    total_act=m.total_act
                    if m.total_act is not None
                    else (
                        (m.a_total_act_energy or 0.0)
                        + (m.b_total_act_energy or 0.0)
                        + (m.c_total_act_energy or 0.0)
                    ),
                    a_total_act_ret_energy=m.a_total_act_ret_energy or 0.0,
                    b_total_act_ret_energy=m.b_total_act_ret_energy or 0.0,
                    c_total_act_ret_energy=m.c_total_act_ret_energy or 0.0,
                    total_act_ret=m.total_act_ret
                    if m.total_act_ret is not None
                    else (
                        (m.a_total_act_ret_energy or 0.0)
                        + (m.b_total_act_ret_energy or 0.0)
                        + (m.c_total_act_ret_energy or 0.0)
                    ),
                    source="alfen",
                )
                self._last_integrate_ts = now
            else:
                self._integrate_locked(now)

        # Persist periodically (every update is fine; volume is tiny)
        self.save()

    @staticmethod
    def _use_alfen_energy(m: AlfenMeasurements) -> bool:
        return any(
            v is not None and v >= 0
            for v in (
                m.a_total_act_energy,
                m.b_total_act_energy,
                m.c_total_act_energy,
                m.total_act,
            )
        )

    def _integrate_locked(self, now: float) -> None:
        """Integrate active power into Wh counters (positive import, negative export)."""
        if self._last_integrate_ts is None:
            self._last_integrate_ts = now
            return
        dt_h = max(0.0, (now - self._last_integrate_ts) / 3600.0)
        self._last_integrate_ts = now
        if dt_h <= 0:
            return

        e = self.state.energy
        e.source = "integrated"

        for phase, attr_power, attr_imp, attr_exp in (
            ("a", self.state.a_act_power, "a_total_act_energy", "a_total_act_ret_energy"),
            ("b", self.state.b_act_power, "b_total_act_energy", "b_total_act_ret_energy"),
            ("c", self.state.c_act_power, "c_total_act_energy", "c_total_act_ret_energy"),
        ):
            p = attr_power
            wh = p * dt_h
            if wh >= 0:
                setattr(e, attr_imp, getattr(e, attr_imp) + wh)
            else:
                setattr(e, attr_exp, getattr(e, attr_exp) + abs(wh))

        tot = self.state.total_act_power * dt_h
        if tot >= 0:
            e.total_act += tot
        else:
            e.total_act_ret += abs(tot)

    def snapshot(self) -> MeterState:
        with self._lock:
            # shallow copy via dataclass fields
            e = self.state.energy
            return MeterState(
                a_voltage=self.state.a_voltage,
                b_voltage=self.state.b_voltage,
                c_voltage=self.state.c_voltage,
                a_current=self.state.a_current,
                b_current=self.state.b_current,
                c_current=self.state.c_current,
                n_current=self.state.n_current,
                a_pf=self.state.a_pf,
                b_pf=self.state.b_pf,
                c_pf=self.state.c_pf,
                a_freq=self.state.a_freq,
                b_freq=self.state.b_freq,
                c_freq=self.state.c_freq,
                a_act_power=self.state.a_act_power,
                b_act_power=self.state.b_act_power,
                c_act_power=self.state.c_act_power,
                total_act_power=self.state.total_act_power,
                a_aprt_power=self.state.a_aprt_power,
                b_aprt_power=self.state.b_aprt_power,
                c_aprt_power=self.state.c_aprt_power,
                total_aprt_power=self.state.total_aprt_power,
                energy=EnergyCounters(**asdict(e)),
                alfen_ok=self.state.alfen_ok,
                alfen_errors=list(self.state.alfen_errors),
                last_update_ts=self.state.last_update_ts,
                start_ts=self.state.start_ts,
            )

    def debug_dict(self) -> Dict[str, Any]:
        s = self.snapshot()
        return {
            "alfen_ok": s.alfen_ok,
            "alfen_errors": s.alfen_errors,
            "last_update_ts": s.last_update_ts,
            "uptime_s": int(time.time() - s.start_ts),
            "power": {
                "a": s.a_act_power,
                "b": s.b_act_power,
                "c": s.c_act_power,
                "total": s.total_act_power,
            },
            "voltage": {"a": s.a_voltage, "b": s.b_voltage, "c": s.c_voltage},
            "current": {"a": s.a_current, "b": s.b_current, "c": s.c_current},
            "energy": asdict(s.energy),
        }
