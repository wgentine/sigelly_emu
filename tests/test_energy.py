# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""Energy counter integration, Alfen lifetime preference, and persistence."""

from __future__ import annotations

import json
import tempfile
import threading
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

    def test_total_wh_follows_the_sum_register_when_phases_disagree(self) -> None:
        """Total Wh tracks total_act_power, not the sum of the phase powers.

        Existing cases use a total that matches a+b+c, so a change that
        derived the total from the phases would still pass. A published sum
        with the opposite sign must import on the phases and export on the total.
        """
        store = self._store()
        sample = _meter(
            a_act_power=1000.0,
            b_act_power=1000.0,
            c_act_power=0.0,
            total_act_power=-500.0,
        )
        store.update_from_alfen(sample)
        self._clock[0] += 3600.0
        store.update_from_alfen(sample)

        energy = store.state.energy
        self.assertAlmostEqual(energy.a_total_act_energy, 1000.0, places=3)
        self.assertAlmostEqual(energy.b_total_act_energy, 1000.0, places=3)
        self.assertAlmostEqual(energy.c_total_act_energy, 0.0, places=3)
        self.assertAlmostEqual(energy.a_total_act_ret_energy, 0.0, places=3)
        self.assertAlmostEqual(energy.total_act, 0.0, places=3)
        self.assertAlmostEqual(energy.total_act_ret, 500.0, places=3)
        self.assertEqual(energy.source, "integrated")

    def test_net_export_credits_the_return_counter_then_import_leaves_it(self) -> None:
        store = self._store()
        exporting = _meter(
            a_act_power=-1000.0,
            b_act_power=-500.0,
            c_act_power=-500.0,
            total_act_power=-2000.0,
        )
        store.update_from_alfen(exporting)
        self._clock[0] += 3600.0
        store.update_from_alfen(exporting)

        energy = store.state.energy
        self.assertAlmostEqual(energy.total_act, 0.0, places=3)
        self.assertAlmostEqual(energy.total_act_ret, 2000.0, places=3)
        self.assertAlmostEqual(energy.a_total_act_energy, 0.0, places=3)
        self.assertAlmostEqual(energy.a_total_act_ret_energy, 1000.0, places=3)
        self.assertAlmostEqual(energy.b_total_act_ret_energy, 500.0, places=3)
        self.assertAlmostEqual(energy.c_total_act_ret_energy, 500.0, places=3)
        self.assertEqual(energy.source, "integrated")

        self._clock[0] += 3600.0
        store.update_from_alfen(_meter(a_act_power=1000.0, total_act_power=1000.0))
        energy = store.state.energy
        self.assertAlmostEqual(energy.total_act, 1000.0, places=3)
        self.assertAlmostEqual(energy.total_act_ret, 2000.0, places=3)
        self.assertAlmostEqual(energy.a_total_act_energy, 1000.0, places=3)
        self.assertAlmostEqual(energy.a_total_act_ret_energy, 1000.0, places=3)

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

    def test_missing_energy_registers_keep_lifetime_totals(self) -> None:
        store = self._store(use_alfen_energy=True)
        store.update_from_alfen(
            _meter(
                a_act_power=3600.0,
                total_act_power=3600.0,
                a_total_act_energy=5000.0,
                total_act=5000.0,
                a_total_act_ret_energy=20.0,
                total_act_ret=20.0,
            )
        )
        self.assertEqual(store.state.energy.source, "alfen")

        self._clock[0] += 1800.0
        store.update_from_alfen(_meter(a_act_power=3600.0, total_act_power=3600.0))

        energy = store.state.energy
        self.assertEqual(energy.source, "integrated")
        self.assertAlmostEqual(energy.total_act, 6800.0, places=3)
        self.assertAlmostEqual(energy.a_total_act_energy, 6800.0, places=3)
        self.assertAlmostEqual(energy.total_act_ret, 20.0, places=3)
        self.assertAlmostEqual(energy.a_total_act_ret_energy, 20.0, places=3)

    def test_return_energy_alone_does_not_replace_counters(self) -> None:
        store = self._store(use_alfen_energy=True)
        store.update_from_alfen(_meter(total_act_power=0.0))
        self._clock[0] += 3600.0
        store.update_from_alfen(
            _meter(
                total_act_power=0.0,
                a_total_act_ret_energy=9999.0,
                total_act_ret=9999.0,
            )
        )
        self.assertEqual(store.state.energy.source, "integrated")
        self.assertEqual(store.state.energy.total_act, 0.0)
        self.assertEqual(store.state.energy.total_act_ret, 0.0)

    def test_explicit_zero_alfen_energy_replaces_counters(self) -> None:
        store = self._store(use_alfen_energy=True)
        store.update_from_alfen(_meter(total_act=100.0, a_total_act_energy=100.0))
        self.assertAlmostEqual(store.state.energy.total_act, 100.0, places=3)
        store.update_from_alfen(_meter(total_act=0.0, a_total_act_energy=0.0))
        self.assertEqual(store.state.energy.total_act, 0.0)
        self.assertEqual(store.state.energy.source, "alfen")

    def test_phase_b_lifetime_energy_replaces_integrated_counters(self) -> None:
        """A later L2 lifetime block must wipe integrated Wh, not add to it.

        Energy registers are often missing until the charger has a session.
        With ALFEN_USE_ENERGY set, the first valid phase counter opts in and
        replaces every counter, including export accumulated on another phase.
        """
        store = self._store(use_alfen_energy=True)
        exporting = _meter(a_act_power=-3600.0, total_act_power=-3600.0)
        store.update_from_alfen(exporting)
        self._clock[0] += 3600.0
        store.update_from_alfen(exporting)
        self.assertAlmostEqual(store.state.energy.total_act_ret, 3600.0, places=3)
        self.assertEqual(store.state.energy.total_act, 0.0)
        self.assertEqual(store.state.energy.source, "integrated")

        # Thirty seconds of the same export must not be added onto the new block.
        self._clock[0] += 30.0
        store.update_from_alfen(
            _meter(
                a_act_power=-3600.0,
                total_act_power=-3600.0,
                b_total_act_energy=80.0,
                b_total_act_ret_energy=2.5,
            )
        )
        energy = store.state.energy
        self.assertEqual(energy.source, "alfen")
        self.assertAlmostEqual(energy.b_total_act_energy, 80.0, places=3)
        self.assertAlmostEqual(energy.total_act, 80.0, places=3)
        self.assertAlmostEqual(energy.a_total_act_energy, 0.0, places=3)
        self.assertAlmostEqual(energy.c_total_act_energy, 0.0, places=3)
        self.assertAlmostEqual(energy.b_total_act_ret_energy, 2.5, places=3)
        self.assertAlmostEqual(energy.total_act_ret, 2.5, places=3)
        self.assertAlmostEqual(energy.a_total_act_ret_energy, 0.0, places=3)
        self.assertAlmostEqual(energy.c_total_act_ret_energy, 0.0, places=3)

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

    def test_counters_hit_disk_on_the_save_interval_only(self) -> None:
        store = self._store(save_interval_s=30.0)
        sample = _meter(a_voltage=230.0, a_act_power=3600.0, total_act_power=3600.0)
        store.update_from_alfen(sample)
        saved = json.loads(Path(self.path).read_text(encoding="utf-8"))
        self.assertEqual(saved["energy"]["total_act"], 0.0)
        self.assertNotIn("a_voltage", saved)

        self._clock[0] += 10.0
        store.update_from_alfen(sample)
        self.assertAlmostEqual(store.state.energy.total_act, 10.0, places=3)
        still_old = json.loads(Path(self.path).read_text(encoding="utf-8"))
        self.assertEqual(still_old["energy"]["total_act"], 0.0)

        self._clock[0] += 25.0
        store.update_from_alfen(sample)
        persisted = json.loads(Path(self.path).read_text(encoding="utf-8"))
        self.assertAlmostEqual(persisted["energy"]["total_act"], 35.0, places=3)
        self.assertEqual(persisted["energy"]["source"], "integrated")

    def test_concurrent_updates_integrate_the_same_interval_once(self) -> None:
        store = self._store()
        sample = _meter(
            a_act_power=3600.0,
            b_act_power=-1800.0,
            c_act_power=0.0,
            total_act_power=1800.0,
        )
        store.update_from_alfen(sample)
        self._clock[0] += 3600.0
        barrier = threading.Barrier(8)

        def worker() -> None:
            barrier.wait()
            store.update_from_alfen(sample)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        energy = store.state.energy
        self.assertAlmostEqual(energy.total_act, 1800.0, places=3)
        self.assertAlmostEqual(energy.total_act_ret, 0.0, places=3)
        self.assertAlmostEqual(energy.a_total_act_energy, 3600.0, places=3)
        self.assertAlmostEqual(energy.b_total_act_ret_energy, 1800.0, places=3)
        self.assertAlmostEqual(energy.b_total_act_energy, 0.0, places=3)
        snap = store.snapshot()
        self.assertAlmostEqual(snap.energy.total_act, energy.total_act, places=3)
        self.assertEqual(snap.energy.source, "integrated")

    def test_save_creates_missing_parent_and_disk_errors_stay_in_memory(self) -> None:
        nested = Path(self._tmp.name) / "nested" / "meter" / "state.json"
        store = EnergyStore(state_path=str(nested), save_interval_s=1e12, use_alfen_energy=False)
        store.state.energy.total_act = 7.25
        store.save()
        reloaded = EnergyStore(state_path=str(nested), save_interval_s=1e12, use_alfen_energy=False)
        self.assertAlmostEqual(reloaded.state.energy.total_act, 7.25, places=3)

        blocked = Path(self._tmp.name) / "not-a-directory"
        blocked.write_text("x", encoding="utf-8")
        store.state_path = blocked / "state.json"
        store._save_interval_s = 1.0
        store._last_save_ts = 0.0
        sample = _meter(a_voltage=231.0, a_act_power=100.0, total_act_power=100.0)
        with self.assertLogs("app.state.energy", level="ERROR"):
            store.update_from_alfen(sample)
        self.assertAlmostEqual(store.state.a_voltage, 231.0, places=3)
        self.assertFalse(store.state_path.exists())

    def test_live_readings_replace_the_previous_sample(self) -> None:
        store = self._store()
        store.update_from_alfen(
            _meter(
                a_voltage=230.1,
                b_voltage=231.2,
                c_voltage=229.3,
                a_current=1.5,
                b_current=-0.5,
                c_current=0.25,
                n_current=0.4,
                a_pf=0.95,
                b_pf=-0.8,
                c_pf=0.7,
                frequency=49.8,
                a_act_power=300.0,
                b_act_power=-100.0,
                c_act_power=50.0,
                total_act_power=250.0,
                a_aprt_power=320.0,
                b_aprt_power=130.0,
                c_aprt_power=80.0,
                total_aprt_power=530.0,
                raw_ok=False,
                errors=["stale"],
            )
        )

        state = store.state
        self.assertFalse(state.alfen_ok)
        self.assertEqual(state.alfen_errors, ["stale"])
        self.assertEqual(state.n_current, 0.4)
        self.assertEqual(state.a_freq, 49.8)
        self.assertEqual(state.b_freq, 49.8)
        self.assertEqual(state.c_freq, 49.8)
        self.assertEqual(state.b_voltage, 231.2)
        self.assertEqual(state.c_act_power, 50.0)
        self.assertEqual(state.b_pf, -0.8)
        self.assertEqual(state.c_aprt_power, 80.0)
        self.assertEqual(state.total_aprt_power, 530.0)
        self.assertEqual(state.last_update_ts, self._clock[0])
        view = store.debug_dict()
        self.assertEqual(view["power"], {"a": 300.0, "b": -100.0, "c": 50.0, "total": 250.0})
        self.assertEqual(view["voltage"], {"a": 230.1, "b": 231.2, "c": 229.3})
        self.assertEqual(view["current"], {"a": 1.5, "b": -0.5, "c": 0.25})
        self.assertEqual(view["alfen_errors"], ["stale"])
        self.assertFalse(view["alfen_ok"])

        self._clock[0] += 5.0
        store.update_from_alfen(
            _meter(
                a_voltage=220.0,
                b_voltage=221.0,
                c_voltage=222.0,
                a_current=0.0,
                b_current=0.0,
                c_current=0.0,
                n_current=None,
                a_pf=0.0,
                b_pf=0.0,
                c_pf=0.0,
                frequency=0.0,
                a_act_power=0.0,
                b_act_power=0.0,
                c_act_power=0.0,
                total_act_power=0.0,
                a_aprt_power=0.0,
                b_aprt_power=0.0,
                c_aprt_power=0.0,
                total_aprt_power=0.0,
            )
        )
        cleared = store.state
        self.assertTrue(cleared.alfen_ok)
        self.assertEqual(cleared.alfen_errors, [])
        self.assertIsNone(cleared.n_current)
        self.assertEqual(cleared.a_freq, 0.0)
        self.assertEqual(cleared.b_freq, 0.0)
        self.assertEqual(cleared.c_freq, 0.0)
        self.assertEqual(cleared.a_voltage, 220.0)
        self.assertEqual(cleared.total_act_power, 0.0)
        self.assertEqual(cleared.last_update_ts, self._clock[0])
        self.assertEqual(store.debug_dict()["alfen_errors"], [])


if __name__ == "__main__":
    unittest.main()
