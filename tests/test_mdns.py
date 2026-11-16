# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""mDNS service records for Shelly discovery, without joining the LAN."""

from __future__ import annotations

import socket
import unittest
from unittest.mock import patch

from app.config import Settings
from app.shelly.mdns import ShellyMdns


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "alfen_host": "127.0.0.1",
        "shelly_advertise_ip": "192.168.30.35",
        "shelly_device_id": "e08cfe96ccc4",
        "shelly_mac": "E0:8C:FE:96:CC:C4",
        "shelly_firmware": "2.0.0",
        "shelly_app": "Pro3EM",
        "http_port": 80,
        "mdns_enable": True,
    }
    base.update(overrides)
    return Settings(**base)


class ShellyMdnsTests(unittest.TestCase):
    def test_disabled_start_does_not_open_a_socket(self) -> None:
        mdns = ShellyMdns(_settings(mdns_enable=False))
        with patch("app.shelly.mdns.Zeroconf") as zeroconf:
            mdns.start()
            zeroconf.assert_not_called()
        mdns.stop()
        status = mdns.status()
        self.assertFalse(status["enabled"])
        self.assertFalse(status["running"])
        self.assertEqual(status["advertise_ip"], "192.168.30.35")
        self.assertEqual(status["port"], 80)
        self.assertEqual(status["services"], [])
        self.assertIsNone(status["last_error"])

    def test_http_and_shelly_records_match_pro3em_discovery(self) -> None:
        mdns = ShellyMdns(_settings())
        http = mdns._make_info("_http._tcp.local.")
        shelly = mdns._make_info("_shelly._tcp.local.")

        self.assertEqual(http.name, "shellypro3em63-e08cfe96ccc4._http._tcp.local.")
        self.assertEqual(shelly.name, "shellypro3em63-e08cfe96ccc4._shelly._tcp.local.")
        self.assertEqual(http.server, "ShellyPro3EM63-E08CFE96CCC4.local.")
        self.assertEqual(shelly.server, "ShellyPro3EM63-E08CFE96CCC4.local.")
        self.assertEqual(http.port, 80)
        self.assertEqual(http.addresses, [socket.inet_aton("192.168.30.35")])
        self.assertEqual(dict(http.properties), {b"gen": b"2"})
        self.assertEqual(
            dict(shelly.properties),
            {b"gen": b"2", b"app": b"Pro3EM", b"ver": b"2.0.0"},
        )


if __name__ == "__main__":
    unittest.main()
