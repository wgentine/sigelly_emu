# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""RPC request validation and Shelly payload edges Sigenstor reads."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.debug.trace import RpcTrace
from app.shelly.responses import (
    build_em_status,
    build_emdata_status,
    build_eth_status,
    build_sys_status,
    dispatch_method,
)
from app.shelly.rpc import ShellyRpcHandler
from app.state.energy import EnergyStore, MeterState
from app.wire_server import _coerce_params, _parse_query


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {"alfen_host": "127.0.0.1"}
    base.update(overrides)
    return Settings(**base)


class RpcHandlerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.trace = RpcTrace()
        store = EnergyStore(
            state_path=str(Path(self._tmp.name) / "state.json"),
            use_alfen_energy=False,
        )
        self.handler = ShellyRpcHandler(_settings(), store, self.trace)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_missing_or_non_string_method_is_invalid_request(self) -> None:
        missing = self.handler.handle_call({"id": 1, "src": "sigen"})
        self.assertEqual(missing["id"], 1)
        self.assertEqual(missing["dst"], "sigen")
        self.assertEqual(missing["src"], "shellypro3em63-349454112233")
        self.assertEqual(missing["error"], {"code": -32600, "message": "Invalid Request"})

        numeric = self.handler.handle_call({"id": 2, "method": 5})
        self.assertEqual(numeric["error"]["code"], -32600)
        self.assertEqual(self.trace.summary()["errors"]["<missing>"], 2)

    def test_unknown_method_is_not_found_and_get_returns_bare_result(self) -> None:
        missing = self.handler.handle_call({"id": 9, "method": "Foo.Bar", "params": ["ignored"]})
        self.assertEqual(missing["error"], {"code": 404, "message": "Method not found"})
        self.assertEqual(self.trace.summary()["errors"]["Foo.Bar"], 1)

        config = self.handler.handle_get_result("EM.GetConfig", {})
        self.assertEqual(config["ct_type"], "3x63A")
        self.assertNotIn("error", config)

        get_missing = self.handler.handle_get_result("Nope", {})
        self.assertEqual(get_missing["error"]["code"], 404)

    def test_batch_preserves_order(self) -> None:
        results = self.handler.handle_batch(
            [
                {"id": 1, "method": "EM.GetConfig"},
                {"id": 2, "method": "Missing.Method"},
            ]
        )
        self.assertEqual(results[0]["id"], 1)
        self.assertIn("result", results[0])
        self.assertEqual(results[1]["id"], 2)
        self.assertEqual(results[1]["error"]["code"], 404)

    def test_dispatch_exception_becomes_internal_error(self) -> None:
        with patch("app.shelly.rpc.dispatch_method", side_effect=RuntimeError("boom")):
            with self.assertLogs("app.shelly.rpc", level="ERROR"):
                result = self.handler.handle_call(
                    {"id": 4, "method": "EM.GetStatus", "src": "sigen"}
                )
        self.assertEqual(result["id"], 4)
        self.assertEqual(result["dst"], "sigen")
        self.assertEqual(result["error"], {"code": -32000, "message": "boom"})
        self.assertNotIn("result", result)
        self.assertEqual(self.trace.summary()["errors"]["EM.GetStatus"], 1)

    def test_components_include_filter(self) -> None:
        state = MeterState()
        settings = _settings()
        config_only = dispatch_method(
            "Shelly.GetComponents",
            settings,
            state,
            {"include": "config"},
        )
        self.assertIn("config", config_only["components"][0])
        self.assertNotIn("status", config_only["components"][0])
        self.assertEqual(config_only["total"], len(config_only["components"]))

        status_only = dispatch_method(
            "Shelly.GetComponents",
            settings,
            state,
            {"include": ["status"]},
        )
        self.assertIn("status", status_only["components"][0])
        self.assertNotIn("config", status_only["components"][0])


class ResponseEdgeTests(unittest.TestCase):
    def test_em_status_errors_only_when_poll_failed(self) -> None:
        state = MeterState(
            a_current=1.0,
            b_current=2.0,
            c_current=0.5,
            alfen_ok=False,
            alfen_errors=["a", "b", "c", "d"],
        )
        failed = build_em_status(state)
        self.assertEqual(failed["errors"], ["a", "b", "c"])
        self.assertIsNone(failed["n_current"])
        self.assertAlmostEqual(failed["total_current"], 3.5, places=3)

        state.alfen_ok = True
        state.n_current = 0.25
        healthy = build_em_status(state)
        self.assertNotIn("errors", healthy)
        self.assertAlmostEqual(healthy["n_current"], 0.25, places=3)

    def test_emdata_rounds_to_centiwatt_hours(self) -> None:
        state = MeterState()
        state.energy.total_act = 10.126
        state.energy.total_act_ret = -10.126
        payload = build_emdata_status(state)
        self.assertEqual(payload["total_act"], 10.13)
        self.assertEqual(payload["total_act_ret"], -10.13)

    def test_eth_link_local_ipv6_from_mac(self) -> None:
        settings = _settings(
            shelly_mac="E0:8C:FE:96:CC:C4",
            shelly_advertise_ip="192.168.30.35",
        )
        eth = build_eth_status(settings)
        self.assertEqual(eth["ip"], "192.168.30.35")
        self.assertEqual(eth["ip6"], ["fe80::e28c:feff:fe96:ccc4"])

        short = _settings(shelly_mac="AA:BB", shelly_advertise_ip="127.0.0.1")
        self.assertEqual(build_eth_status(short)["ip6"], ["fe80::"])

    def test_invalid_timezone_falls_back_to_utc(self) -> None:
        status = build_sys_status(
            _settings(shelly_tz="Not/AZone"),
            MeterState(start_ts=1_700_000_000.0, last_update_ts=1_700_000_000.0),
        )
        self.assertEqual(status["utc_offset"], 0)
        self.assertRegex(status["time"], r"^\d{2}:\d{2}$")
        self.assertEqual(status["last_sync_ts"], 1_700_000_000)


class RpcTraceTests(unittest.TestCase):
    def test_ring_keeps_newest_and_counts_errors(self) -> None:
        trace = RpcTrace(maxlen=2)
        trace.record("10.0.0.1", "EM.GetStatus", "POST", True)
        trace.record("10.0.0.2", "Nope", "GET", False)
        trace.record("10.0.0.3", "EM.GetConfig", "WS", True)

        self.assertEqual([event["method"] for event in trace.recent()], ["EM.GetConfig", "Nope"])
        summary = trace.summary()
        self.assertEqual(summary["total_calls"], 3)
        self.assertEqual(summary["methods"]["EM.GetStatus"], 1)
        self.assertEqual(summary["errors"], {"Nope": 1})
        self.assertEqual(len(summary["recent"]), 2)
        self.assertTrue(summary["recent"][0]["ok"])
        self.assertEqual(summary["recent"][0]["client_ip"], "10.0.0.3")
        self.assertFalse(summary["recent"][1]["ok"])

    def test_concurrent_records_do_not_drop_counts(self) -> None:
        trace = RpcTrace()

        def worker(ok: bool) -> None:
            for _ in range(100):
                trace.record("10.0.0.1", "EM.GetStatus", "POST", ok)

        threads = [threading.Thread(target=worker, args=(index % 2 == 0,)) for index in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        summary = trace.summary()
        self.assertEqual(summary["total_calls"], 400)
        self.assertEqual(summary["methods"]["EM.GetStatus"], 400)
        self.assertEqual(summary["errors"]["EM.GetStatus"], 200)
        self.assertEqual(len(trace.recent(1000)), 200)


class WireQueryTests(unittest.TestCase):
    def test_parse_query(self) -> None:
        self.assertEqual(_parse_query("/shelly"), ("/shelly", {}))
        path, params = _parse_query("/rpc?method=EM.GetStatus&id=2&flag&")
        self.assertEqual(path, "/rpc")
        self.assertEqual(params, {"method": "EM.GetStatus", "id": "2", "flag": ""})

    def test_coerce_params(self) -> None:
        parsed = _coerce_params(
            {
                "method": "EM.GetStatus",
                "batch": "[]",
                "id": "7",
                "gain": "1.5",
                "offset": "-3",
                "name": "meter",
                "params": '{"include":["status"]}',
            }
        )
        self.assertEqual(parsed["id"], 7)
        self.assertEqual(parsed["gain"], 1.5)
        self.assertEqual(parsed["offset"], -3.0)
        self.assertEqual(parsed["name"], "meter")
        self.assertEqual(parsed["include"], ["status"])
        self.assertNotIn("method", parsed)
        self.assertNotIn("batch", parsed)

    def test_coerce_ignores_invalid_params_json(self) -> None:
        self.assertEqual(_coerce_params({"params": "{", "id": "0"}), {"id": 0})
        self.assertEqual(_coerce_params({"params": "[1,2]", "id": "1"}), {"id": 1})


if __name__ == "__main__":
    unittest.main()
