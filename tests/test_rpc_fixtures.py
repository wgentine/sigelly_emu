# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""RPC payload shape checks against real Shelly capture fixtures."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.config import Settings
from app.shelly.responses import (
    build_device_info,
    build_em_status,
    build_shelly_http_info,
    dispatch_method,
)
from app.state.energy import MeterState

_FIXTURES = Path(__file__).parent / "fixtures" / "real_shelly"


def _load(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def _fixture_settings() -> Settings:
    """Settings aligned to the real device captured in tests/fixtures/real_shelly/."""
    return Settings(
        alfen_host="127.0.0.1",
        shelly_device_id="e08cfe96ccc4",
        shelly_mac="E0:8C:FE:96:CC:C4",
        shelly_model="SPEM-003CEBEU63",
        shelly_firmware="2.0.0",
        shelly_fw_id="20260710-101221/2.0.0-g87fbfa4",
        shelly_app="Pro3EM",
    )


def _meter_from_em_fixture(data: dict) -> MeterState:
    state = MeterState(last_update_ts=1_700_000_000.0)
    for key in (
        "a_voltage",
        "b_voltage",
        "c_voltage",
        "a_current",
        "b_current",
        "c_current",
        "a_pf",
        "b_pf",
        "c_pf",
        "a_freq",
        "b_freq",
        "c_freq",
        "a_act_power",
        "b_act_power",
        "c_act_power",
        "total_act_power",
        "a_aprt_power",
        "b_aprt_power",
        "c_aprt_power",
        "total_aprt_power",
    ):
        if key in data:
            setattr(state, key, float(data[key]))
    state.n_current = data.get("n_current")
    return state


class RpcFixtureTests(unittest.TestCase):
    def test_shelly_endpoint_matches_fixture_keys(self) -> None:
        fixture = _load("shelly.json")
        settings = _fixture_settings()
        state = MeterState()
        payload = build_shelly_http_info(settings, state)
        for key in fixture:
            self.assertIn(key, payload, msg=f"missing key {key}")
        self.assertEqual(payload["model"], fixture["model"])
        self.assertEqual(payload["gen"], fixture["gen"])
        self.assertFalse(payload["auth_en"])
        self.assertEqual(payload["mac"], fixture["mac"])
        self.assertEqual(payload["fw_id"], fixture["fw_id"])

    def test_em_get_status_fields_match_fixture(self) -> None:
        fixture = _load("rpc_EM.GetStatus_id_0.json")
        state = _meter_from_em_fixture(fixture)
        result = build_em_status(state)
        for key in fixture:
            if key == "id":
                continue
            self.assertIn(key, result, msg=f"missing {key}")
        self.assertAlmostEqual(result["a_voltage"], fixture["a_voltage"], places=1)
        self.assertAlmostEqual(result["total_act_power"], fixture["total_act_power"], places=1)

    def test_dispatch_em_get_status(self) -> None:
        fixture = _load("rpc_EM.GetStatus_id_0.json")
        state = _meter_from_em_fixture(fixture)
        settings = _fixture_settings()
        result = dispatch_method("EM.GetStatus", settings, state)
        self.assertIn("total_act_power", result)

    def test_wifi_alias_routes_to_wifi_get_status(self) -> None:
        settings = _fixture_settings()
        state = MeterState()
        result = dispatch_method("WiFi.GetStatus", settings, state)
        self.assertIn("status", result)
        self.assertIn("sta_ip", result)

    def test_device_info_static_fields(self) -> None:
        settings = _fixture_settings()
        info = build_device_info(settings)
        self.assertEqual(info["profile"], "triphase")
        self.assertEqual(info["provision"], "complete")
        self.assertFalse(info["enhanced_security"])


if __name__ == "__main__":
    unittest.main()
