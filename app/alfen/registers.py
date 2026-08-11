# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""Alfen Eve Pro socket measurement register map and float decode helpers."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import List, Optional


# Contiguous holding-register window for socket measurements (slave ID 1).
REGISTER_START = 306
REGISTER_COUNT = 104  # 306..409 inclusive


@dataclass
class AlfenMeasurements:
    """Decoded socket measurements mapped toward Shelly EM fields."""

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
    frequency: float = 50.0
    a_act_power: float = 0.0
    b_act_power: float = 0.0
    c_act_power: float = 0.0
    total_act_power: float = 0.0
    a_aprt_power: float = 0.0
    b_aprt_power: float = 0.0
    c_aprt_power: float = 0.0
    total_aprt_power: float = 0.0
    # Energy delivered (import / active energy) Wh
    a_total_act_energy: Optional[float] = None
    b_total_act_energy: Optional[float] = None
    c_total_act_energy: Optional[float] = None
    total_act: Optional[float] = None
    # Energy consumed returned / export Wh
    a_total_act_ret_energy: Optional[float] = None
    b_total_act_ret_energy: Optional[float] = None
    c_total_act_ret_energy: Optional[float] = None
    total_act_ret: Optional[float] = None
    raw_ok: bool = False
    errors: List[str] = field(default_factory=list)


def _is_nan_regs(regs: List[int]) -> bool:
    """Alfen fills unavailable registers with 0xFFFF."""
    return all(r == 0xFFFF for r in regs)


def decode_float32(regs: List[int], offset: int) -> Optional[float]:
    """Decode big-endian FLOAT32 from two 16-bit holding registers."""
    chunk = regs[offset : offset + 2]
    if len(chunk) < 2 or _is_nan_regs(chunk):
        return None
    raw = struct.pack(">HH", chunk[0] & 0xFFFF, chunk[1] & 0xFFFF)
    value = struct.unpack(">f", raw)[0]
    if value != value:  # NaN
        return None
    return float(value)


def decode_float64(regs: List[int], offset: int) -> Optional[float]:
    """Decode big-endian FLOAT64 from four 16-bit holding registers."""
    chunk = regs[offset : offset + 4]
    if len(chunk) < 4 or _is_nan_regs(chunk):
        return None
    raw = struct.pack(
        ">HHHH",
        chunk[0] & 0xFFFF,
        chunk[1] & 0xFFFF,
        chunk[2] & 0xFFFF,
        chunk[3] & 0xFFFF,
    )
    value = struct.unpack(">d", raw)[0]
    if value != value:
        return None
    return float(value)


def _f(regs: List[int], address: int, default: float = 0.0) -> float:
    """Read FLOAT32 at absolute Modbus address within the 306.. window."""
    offset = address - REGISTER_START
    value = decode_float32(regs, offset)
    return default if value is None else value


def _d(regs: List[int], address: int) -> Optional[float]:
    offset = address - REGISTER_START
    return decode_float64(regs, offset)


def _split_by_current(
    total: float, a_current: float, b_current: float, c_current: float
) -> tuple[float, float, float]:
    """Split a sum quantity across phases proportional to |I| (or all on A)."""
    weights = (abs(a_current), abs(b_current), abs(c_current))
    weight_sum = sum(weights)
    if weight_sum <= 0.0:
        return total, 0.0, 0.0
    return (
        total * (weights[0] / weight_sum),
        total * (weights[1] / weight_sum),
        total * (weights[2] / weight_sum),
    )


def _distribute_missing_phase_powers(m: AlfenMeasurements) -> None:
    """Fill missing per-phase active power from the sum register + currents."""
    if m.total_act_power == 0.0:
        return
    if m.a_act_power or m.b_act_power or m.c_act_power:
        return
    m.a_act_power, m.b_act_power, m.c_act_power = _split_by_current(
        m.total_act_power, m.a_current, m.b_current, m.c_current
    )


def _distribute_missing_aprt_powers(m: AlfenMeasurements) -> None:
    """Fill missing per-phase apparent power from sum or V*I."""
    if m.total_aprt_power == 0.0:
        m.a_aprt_power = abs(m.a_voltage * m.a_current)
        m.b_aprt_power = abs(m.b_voltage * m.b_current)
        m.c_aprt_power = abs(m.c_voltage * m.c_current)
        m.total_aprt_power = m.a_aprt_power + m.b_aprt_power + m.c_aprt_power
        return
    if m.a_aprt_power or m.b_aprt_power or m.c_aprt_power:
        return
    m.a_aprt_power, m.b_aprt_power, m.c_aprt_power = _split_by_current(
        m.total_aprt_power, m.a_current, m.b_current, m.c_current
    )


def _fill_power_factor(m: AlfenMeasurements) -> None:
    """Derive PF from P/(V*I) when Alfen leaves PF registers unavailable."""

    def _pf(power: float, voltage: float, current: float, existing: float) -> float:
        if existing:
            return existing
        denom = abs(voltage * current)
        if denom <= 1e-6:
            return 0.0
        return max(-1.0, min(1.0, power / denom))

    m.a_pf = _pf(m.a_act_power, m.a_voltage, m.a_current, m.a_pf)
    m.b_pf = _pf(m.b_act_power, m.b_voltage, m.b_current, m.b_pf)
    m.c_pf = _pf(m.c_act_power, m.c_voltage, m.c_current, m.c_pf)


def parse_registers(regs: List[int]) -> AlfenMeasurements:
    """Parse a contiguous register block starting at 306 into measurements."""
    if len(regs) < REGISTER_COUNT:
        return AlfenMeasurements(
            raw_ok=False,
            errors=[f"short_read: got {len(regs)} expected {REGISTER_COUNT}"],
        )

    m = AlfenMeasurements(raw_ok=True)

    m.a_voltage = _f(regs, 306)
    m.b_voltage = _f(regs, 308)
    m.c_voltage = _f(regs, 310)

    n_cur = decode_float32(regs, 318 - REGISTER_START)
    m.n_current = n_cur

    m.a_current = _f(regs, 320)
    m.b_current = _f(regs, 322)
    m.c_current = _f(regs, 324)

    m.a_pf = _f(regs, 328)
    m.b_pf = _f(regs, 330)
    m.c_pf = _f(regs, 332)

    m.frequency = _f(regs, 336, default=50.0)

    m.a_act_power = _f(regs, 338)
    m.b_act_power = _f(regs, 340)
    m.c_act_power = _f(regs, 342)
    m.total_act_power = _f(regs, 344)
    # If sum register is NaN/zero but phases have power, synthesize total
    if m.total_act_power == 0.0 and (m.a_act_power or m.b_act_power or m.c_act_power):
        m.total_act_power = m.a_act_power + m.b_act_power + m.c_act_power
    # Alfen often leaves per-phase active power as 0xFFFF while publishing
    # sum power + phase currents (typical for single-phase charging).
    _distribute_missing_phase_powers(m)

    m.a_aprt_power = _f(regs, 346)
    m.b_aprt_power = _f(regs, 348)
    m.c_aprt_power = _f(regs, 350)
    m.total_aprt_power = _f(regs, 352)
    if m.total_aprt_power == 0.0 and (m.a_aprt_power or m.b_aprt_power or m.c_aprt_power):
        m.total_aprt_power = m.a_aprt_power + m.b_aprt_power + m.c_aprt_power
    _distribute_missing_aprt_powers(m)

    _fill_power_factor(m)

    # Real Energy Delivered L1/L2/L3/Sum (Wh) — FLOAT64
    m.a_total_act_energy = _d(regs, 362)
    m.b_total_act_energy = _d(regs, 366)
    m.c_total_act_energy = _d(regs, 370)
    m.total_act = _d(regs, 374)

    # Real Energy Consumed L1/L2/L3/Sum (Wh) — used as returned/export
    m.a_total_act_ret_energy = _d(regs, 378)
    m.b_total_act_ret_energy = _d(regs, 382)
    m.c_total_act_ret_energy = _d(regs, 386)
    m.total_act_ret = _d(regs, 390)

    return m
