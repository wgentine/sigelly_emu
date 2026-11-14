# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""Energy counter integration, Alfen lifetime preference, and persistence."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import app.state.energy as energy_mod
from app.alfen.registers import AlfenMeasurements
from app.state.energy import EnergyStore


def _meter(**kwargs: object) -> AlfenMeasurements:
    measurement = AlfenMeasurements(raw_ok=True, frequency=50.0)
    for key, value in kwargs.items():
        setattr(measurement, key, value)
    return measurement


class EnergyStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self._tmp.name) / "state.json")
        self._clock = [1_700_000_000.0]
        self._real_time = energy_mod.time.time
        energy_mod.time.time = lambda: self._clock[0]

    def tearDown(self) -> None:
        energy_mod.time.time = self._real_time
        self._tmp.cleanup()

    def _store(self, **kwargs: object) -> EnergyStore:
        options: dict[str, object] = {
            "state_path": self.path,
            "save_interval_s": 1e12,
            "use_alfen_energy": False,
        }
        options.update(kwargs)
        return EnergyStore(**options)

    def test_import_and_export_integrate_on_separate_counters(self) -> None:
        store = self._store()
        sample = _meter(
            a_act_power=1000.0,
            b_act_power=-500.0,
            c_act_power=0.0,
            total_act_power=500.0,
        )
        store.update_from_alfen(sample)
        self.assertEqual(store.state.energy.total_act, 0.0)
        self.assertEqual(store.state.energy.total_act_ret, 0.0)
        self.assertEqual(store.state.energy.source, "integrated")

        # Same timestamp is a zero-length interval and must not add energy.
        store.update_from_alfen(sample)
        self.assertEqual(store.state.energy.total_act, 0.0)

        self._clock[0] += 3600.0
        store.update_from_alfen(sample)
        energy = store.state.energy
        self.assertAlmostEqual(energy.a_total_act_energy, 1000.0, places=3)
        self.assertAlmostEqual(energy.b_total_act_ret_energy, 500.0, places=3)
        self.assertAlmostEqual(energy.b_total_act_energy, 0.0, places=3)
        self.assertAlmostEqual(energy.total_act, 500.0, places=3)
        self.assertAlmostEqual(energy.total_act_ret, 0.0, places=3)

    def test_clock_step_backwards_does_not_change_counters(self) -> None:
        store = self._store()
        sample = _meter(a_act_power=1000.0, total_act_power=1000.0)
        store.update_from_alfen(sample)
        self._clock[0] += 3600.0
        store.update_from_alfen(sample)
        self.assertAlmostEqual(store.state.energy.total_act, 1000.0, places=3)

        self._clock[0] -= 1800.0
        store.update_from_alfen(sample)
        self.assertAlmostEqual(store.state.energy.total_act, 1000.0, places=3)
        self.assertAlmostEqual(store.state.energy.total_act_ret, 0.0, places=3)

    def test_alfen_lifetime_energy_ignored_unless_enabled(self) -> None:
        sample = _meter(
            a_act_power=1000.0,
            total_act_power=1000.0,
            a_total_act_energy=5_000_000.0,
            total_act=5_000_000.0,
        )
        integrated = self._store(use_alfen_energy=False)
        integrated.update_from_alfen(sample)
        self._clock[0] += 3600.0
        integrated.update_from_alfen(sample)
        self.assertAlmostEqual(integrated.state.energy.total_act, 1000.0, places=3)
        self.assertEqual(integrated.state.energy.source, "integrated")

        self._clock[0] += 10.0
        copied = self._store(use_alfen_energy=True)
        copied.update_from_alfen(sample)
        self.assertAlmostEqual(copied.state.energy.total_act, 5_000_000.0, places=3)
        self.assertEqual(copied.state.energy.source, "alfen")

        self._clock[0] += 3600.0
        later = _meter(
            a_act_power=10_000.0,
            total_act_power=10_000.0,
            a_total_act_energy=5_000_100.0,
            total_act=5_000_100.0,
        )
        copied.update_from_alfen(later)
        self.assertAlmostEqual(copied.state.energy.total_act, 5_000_100.0, places=3)
        self.assertEqual(copied.state.energy.source, "alfen")

    def test_missing_alfen_total_is_sum_of_phases(self) -> None:
        store = self._store(use_alfen_energy=True)
        store.update_from_alfen(
            _meter(
                a_total_act_energy=1.5,
                b_total_act_energy=2.5,
                c_total_act_energy=None,
                total_act=None,
                a_total_act_ret_energy=0.5,
                b_total_act_ret_energy=None,
                c_total_act_ret_energy=0.25,
                total_act_ret=None,
            )
        )
        energy = store.state.energy
        self.assertAlmostEqual(energy.total_act, 4.0, places=3)
        self.assertAlmostEqual(energy.total_act_ret, 0.75, places=3)
        self.assertEqual(energy.source, "alfen")

    def test_negative_alfen_energy_falls_back_to_integration(self) -> None:
        store = self._store(use_alfen_energy=True)
        sample = _meter(
            a_act_power=3600.0,
            total_act_power=3600.0,
            a_total_act_energy=-5.0,
            total_act=None,
        )
        store.update_from_alfen(sample)
        self._clock[0] += 3600.0
        store.update_from_alfen(sample)
        self.assertAlmostEqual(store.state.energy.total_act, 3600.0, places=3)
        self.assertEqual(store.state.energy.source, "integrated")

    def test_explicit_zero_alfen_energy_replaces_counters(self) -> None:
        store = self._store(use_alfen_energy=True)
        store.update_from_alfen(_meter(total_act=100.0, a_total_act_energy=100.0))
        self.assertAlmostEqual(store.state.energy.total_act, 100.0, places=3)
        store.update_from_alfen(_meter(total_act=0.0, a_total_act_energy=0.0))
        self.assertEqual(store.state.energy.total_act, 0.0)
        self.assertEqual(store.state.energy.source, "alfen")

    def test_only_energy_counters_survive_reload(self) -> None:
        store = self._store()
        store.update_from_alfen(_meter(a_voltage=230.0, total_act_power=0.0))
        store.state.energy.total_act = 80.5
        store.state.energy.total_act_ret = 1.25
        store.state.a_voltage = 230.0
        store.save()

        loaded = self._store()
        self.assertAlmostEqual(loaded.state.energy.total_act, 80.5, places=3)
        self.assertAlmostEqual(loaded.state.energy.total_act_ret, 1.25, places=3)
        self.assertEqual(loaded.state.a_voltage, 0.0)
        self.assertFalse(loaded.state.alfen_ok)

    def test_partial_and_corrupt_state_files_do_not_crash(self) -> None:
        Path(self.path).write_text(
            json.dumps({"energy": {"total_act": 42}}),
            encoding="utf-8",
        )
        partial = self._store()
        self.assertAlmostEqual(partial.state.energy.total_act, 42.0, places=3)
        self.assertEqual(partial.state.energy.a_total_act_energy, 0.0)
        self.assertEqual(partial.state.energy.source, "integrated")

        Path(self.path).write_text("{not-json", encoding="utf-8")
        with self.assertLogs("app.state.energy", level="ERROR"):
            broken = self._store()
        self.assertEqual(broken.state.energy.total_act, 0.0)

        Path(self.path).write_text(json.dumps({"energy": ["nope"]}), encoding="utf-8")
        with self.assertLogs("app.state.energy", level="ERROR"):
            wrong_type = self._store()
        self.assertEqual(wrong_type.state.energy.total_act, 0.0)

        Path(self.path).write_text(
            json.dumps({"energy": {"total_act": "nope"}}),
            encoding="utf-8",
        )
        with self.assertLogs("app.state.energy", level="ERROR"):
            bad_number = self._store()
        self.assertEqual(bad_number.state.energy.total_act, 0.0)

    def test_snapshot_and_error_list_are_isolated(self) -> None:
        store = self._store()
        measurement = _meter(a_voltage=230.0, errors=["boom"])
        measurement.raw_ok = False
        store.update_from_alfen(measurement)
        measurement.errors.append("later")

        snap = store.snapshot()
        snap.a_voltage = 1.0
        snap.energy.total_act = 999.0
        snap.alfen_errors.append("snap")

        self.assertEqual(store.state.a_voltage, 230.0)
        self.assertEqual(store.state.energy.total_act, 0.0)
        self.assertEqual(store.state.alfen_errors, ["boom"])
        self.assertFalse(store.state.alfen_ok)


if __name__ == "__main__":
    unittest.main()
