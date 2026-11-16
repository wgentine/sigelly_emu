# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""Shelly identity strings and advertise-address selection."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from app.config import Settings, detect_lan_ip


class SettingsIdentityTests(unittest.TestCase):
    def test_hostname_and_explicit_advertise_address(self) -> None:
        settings = Settings(
            alfen_host="10.0.0.5",
            shelly_advertise_ip="10.1.2.3",
            shelly_device_id="AbC",
            shelly_mac="aa:bb:cc:dd:ee:ff",
        )
        self.assertEqual(settings.advertise_ip, "10.1.2.3")
        self.assertEqual(settings.device_hostname, "shellypro3em63-abc")
        self.assertEqual(settings.mac_no_colons, "AABBCCDDEEFF")
        self.assertEqual(settings.mdns_server_hostname, "ShellyPro3EM63-AABBCCDDEEFF")

    def test_lan_ip_uses_udp_socket_and_falls_back_when_it_fails(self) -> None:
        with patch("app.config.socket.socket") as factory:
            sock = factory.return_value
            sock.__enter__.return_value = sock
            sock.getsockname.return_value = ("192.168.5.5", 9999)
            self.assertEqual(detect_lan_ip(), "192.168.5.5")
            sock.connect.assert_called_once_with(("8.8.8.8", 80))

            sock.connect.side_effect = OSError("down")
            self.assertEqual(detect_lan_ip(), "127.0.0.1")

        settings = Settings(alfen_host="10.0.0.5", shelly_advertise_ip=None)
        with patch("app.config.detect_lan_ip", return_value="192.168.8.8"):
            self.assertEqual(settings.advertise_ip, "192.168.8.8")


if __name__ == "__main__":
    unittest.main()
