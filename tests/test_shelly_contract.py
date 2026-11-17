# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""Enrollment payloads Sigenstor reads while pairing a Pro 3EM."""

from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from app.config import Settings
from app.shelly.responses import (
    SUPPORTED_METHODS,
    build_em_status,
    build_get_config,
    build_sys_status,
    build_wifi_status,
    dispatch_method,
)
from app.state.energy import MeterState

_IMPLEMENTED = (
    "Shelly.GetDeviceInfo",
    "Shelly.GetStatus",
    "Shelly.GetConfig",
    "Shelly.ListMethods",
    "Shelly.GetComponents",
    "EM.GetStatus",
    "EM.GetConfig",
    "EMData.GetStatus",
    "EMData.GetConfig",
    "Wifi.GetStatus",
    "Sys.GetStatus",
    "Eth.GetStatus",
    "Cloud.GetStatus",
    "Mqtt.GetStatus",
    "WS.GetStatus",
    "BLE.GetStatus",
    "Modbus.GetStatus",
    "Temperature.GetStatus",
)


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "alfen_host": "127.0.0.1",
        "shelly_advertise_ip": "192.168.30.35",
        "shelly_device_id": "e08cfe96ccc4",
        "shelly_mac": "E0:8C:FE:96:CC:C4",
        "shelly_tz": "Europe/Amsterdam",
        "shelly_lat": 52.3346,
        "shelly_lon": 4.8914,
    }
    base.update(overrides)
    return Settings(**base)


class ShellyContractTests(unittest.TestCase):
    def test_get_config_enrollment_fields(self) -> None:
        settings = _settings()
        config = build_get_config(settings)
        hostname = "shellypro3em63-e08cfe96ccc4"

        self.assertEqual(config["em:0"]["ct_type"], "3x63A")
        self.assertTrue(config["modbus"]["enable"])
        self.assertEqual(config["wifi"]["ap"]["ssid"], "ShellyPro3EM63-E08CFE96CCC4")
        self.assertFalse(config["wifi"]["ap"]["enable"])
        self.assertIsNone(config["wifi"]["sta"]["ssid"])
        self.assertFalse(config["wifi"]["sta"]["enable"])
        self.assertEqual(config["mqtt"]["client_id"], hostname)
        self.assertEqual(config["mqtt"]["topic_prefix"], hostname)
        self.assertFalse(config["mqtt"]["enable"])
        self.assertEqual(config["sys"]["device"]["mac"], "E08CFE96CCC4")
        self.assertTrue(config["sys"]["device"]["discoverable"])
        self.assertEqual(config["sys"]["device"]["profile"], "triphase")
        self.assertEqual(config["sys"]["location"]["tz"], "Europe/Amsterdam")
        self.assertEqual(config["sys"]["location"]["lat"], 52.3346)
        self.assertEqual(config["sys"]["location"]["lon"], 4.8914)

        configured = build_get_config(_settings(shelly_wifi_ssid="lab-net"))
        self.assertEqual(configured["wifi"]["sta"]["ssid"], "lab-net")
        blank = build_get_config(_settings(shelly_wifi_ssid=""))
        self.assertIsNone(blank["wifi"]["sta"]["ssid"])

    def test_wifi_status_stays_disconnected_on_ethernet_only_firmware(self) -> None:
        status = build_wifi_status(_settings(shelly_wifi_ssid="lab-net"))
        self.assertEqual(status["status"], "disconnected")
        self.assertIsNone(status["ssid"])
        self.assertEqual(status["sta_ip"], "0.0.0.0")
        self.assertIsNone(status["sta_ip6"])

    def test_aliases_default_components_and_dispatch_table(self) -> None:
        settings = _settings()
        state = MeterState()
        for method in _IMPLEMENTED:
            result = dispatch_method(method, settings, state, {})
            self.assertIsInstance(result, dict, msg=method)

        self.assertEqual(
            dispatch_method("MQTT.GetStatus", settings, state),
            {"connected": False},
        )
        self.assertEqual(
            dispatch_method("Mqtt.GetStatus", settings, state),
            dispatch_method("MQTT.GetStatus", settings, state),
        )
        self.assertEqual(dispatch_method("Ws.GetStatus", settings, state), {"connected": False})
        self.assertEqual(dispatch_method("Ble.GetStatus", settings, state), {})
        info = dispatch_method("GetDeviceInfo", settings, state)
        self.assertEqual(info["id"], "shellypro3em63-e08cfe96ccc4")
        self.assertEqual(dispatch_method("GetConfig", settings, state)["modbus"]["enable"], True)

        full = dispatch_method("GetStatus", settings, state)
        self.assertEqual(full["eth"]["ip"], "192.168.30.35")
        self.assertEqual(full["temperature:0"]["tC"], 45.0)
        self.assertIn("emdata:0", full)

        default = dispatch_method("Shelly.GetComponents", settings, state, {})
        self.assertIn("status", default["components"][0])
        self.assertNotIn("config", default["components"][0])
        self.assertEqual(default["offset"], 0)
        self.assertEqual(default["total"], len(default["components"]))

        both = dispatch_method(
            "Shelly.GetComponents",
            settings,
            state,
            {"include": ["status", "config"]},
        )
        self.assertIn("status", both["components"][0])
        self.assertIn("config", both["components"][0])

        unknown = dispatch_method("Shelly.GetComponents", settings, state, {"include": "nope"})
        self.assertNotIn("status", unknown["components"][0])
        self.assertNotIn("config", unknown["components"][0])
        self.assertEqual(unknown["components"][0]["key"], "ble")

    def test_em_status_rounding_and_absent_errors(self) -> None:
        state = MeterState(
            a_voltage=228.26,
            a_act_power=10.26,
            a_pf=0.996,
            a_current=1.2346,
            b_current=0.0,
            c_current=0.0,
            total_act_power=10.1236,
            alfen_ok=False,
            alfen_errors=[],
        )
        payload = build_em_status(state)
        self.assertEqual(payload["a_voltage"], 228.3)
        self.assertEqual(payload["a_act_power"], 10.3)
        self.assertEqual(payload["a_pf"], 1.0)
        self.assertEqual(payload["a_current"], 1.235)
        self.assertEqual(payload["total_act_power"], 10.124)
        self.assertEqual(payload["total_current"], 1.235)
        self.assertEqual(payload["user_calibrated_phase"], [])
        self.assertNotIn("errors", payload)

    def test_sys_status_uses_meter_clock_and_falls_back_when_unsynced(self) -> None:
        real_datetime = datetime

        class Frozen(real_datetime):
            @classmethod
            def now(cls, tz=None):  # type: ignore[override]
                return real_datetime(2026, 11, 16, 12, 30, tzinfo=tz)

        settings = _settings()
        with (
            patch("app.shelly.responses.time.time", return_value=1_763_292_600.0),
            patch("app.shelly.responses.datetime", Frozen),
        ):
            unsynced = build_sys_status(
                settings,
                MeterState(start_ts=1_763_292_000.0, last_update_ts=0.0),
            )
            synced = build_sys_status(
                settings,
                MeterState(start_ts=1_763_292_000.0, last_update_ts=1_763_292_100.0),
            )

        self.assertEqual(unsynced["time"], "12:30")
        self.assertEqual(unsynced["utc_offset"], 3600)
        self.assertEqual(unsynced["unixtime"], 1_763_292_600)
        self.assertEqual(unsynced["uptime"], 600)
        self.assertEqual(unsynced["last_sync_ts"], 1_763_292_600)
        self.assertEqual(synced["last_sync_ts"], 1_763_292_100)
        self.assertEqual(unsynced["mac"], "E08CFE96CCC4")
        self.assertFalse(unsynced["restart_required"])

    def test_list_methods_advertises_the_meter_rpcs(self) -> None:
        methods = dispatch_method("Shelly.ListMethods", _settings(), MeterState())["methods"]
        self.assertEqual(methods, list(SUPPORTED_METHODS))
        for required in ("Shelly.GetDeviceInfo", "Shelly.GetConfig", "EM.GetStatus", "EMData.GetStatus"):
            self.assertIn(required, methods)
        self.assertGreater(len(methods), 50)

    def test_list_methods_falls_back_when_the_catalog_is_unreadable(self) -> None:
        from app.shelly import responses

        with patch.object(responses.Path, "read_text", side_effect=OSError("gone")):
            missing = responses._load_list_methods()
        self.assertIn("EM.GetStatus", missing)
        self.assertIn("Shelly.GetDeviceInfo", missing)
        self.assertLess(len(missing), 30)

        with patch.object(responses.Path, "read_text", return_value="{not-json"):
            self.assertIn("EMData.GetStatus", responses._load_list_methods())

        with patch.object(responses.Path, "read_text", return_value='{"methods": []}'):
            self.assertIn("Wifi.GetStatus", responses._load_list_methods())

        with patch.object(responses.Path, "read_text", return_value="[1, 2]"):
            self.assertIn("Sys.GetStatus", responses._load_list_methods())


if __name__ == "__main__":
    unittest.main()
