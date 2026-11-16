# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""mDNS register/unregister lifecycle without joining the LAN.

Record shape is checked on the ServiceInfo objects start() actually passes
to Zeroconf, and the installed register_service signature is checked so a
zeroconf bump cannot drop ttl / allow_name_change silently.
"""

from __future__ import annotations

import inspect
import socket
import unittest
from unittest.mock import patch

from zeroconf import Zeroconf

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


class _FakeZeroconf:
    def __init__(self) -> None:
        self.registered: list[tuple[object, int | None, bool]] = []
        self.unregistered: list[object] = []
        self.closed = False
        self.raise_on: set[str] = set()

    def register_service(
        self,
        info: object,
        ttl: int | None = None,
        allow_name_change: bool = False,
    ) -> None:
        name = getattr(info, "name", "")
        if any(token in name for token in self.raise_on):
            raise OSError(f"register failed for {name}")
        self.registered.append((info, ttl, allow_name_change))

    def unregister_service(self, info: object) -> None:
        if "unregister" in self.raise_on:
            raise OSError("unregister failed")
        self.unregistered.append(info)

    def close(self) -> None:
        if "close" in self.raise_on:
            raise OSError("close failed")
        self.closed = True


class MdnsLifecycleTests(unittest.TestCase):
    def test_register_service_still_accepts_probe_kwargs(self) -> None:
        params = inspect.signature(Zeroconf.register_service).parameters
        self.assertIn("ttl", params)
        self.assertIn("allow_name_change", params)

    def test_start_registers_both_records_and_is_idempotent(self) -> None:
        created: list[_FakeZeroconf] = []

        class Recording(_FakeZeroconf):
            def __init__(self) -> None:
                super().__init__()
                created.append(self)

        mdns = ShellyMdns(_settings())
        with patch("app.shelly.mdns.Zeroconf", Recording):
            mdns.start()
            mdns.start()

        self.assertEqual(len(created), 1)
        zc = created[0]
        self.assertEqual(len(zc.registered), 2)
        by_type = {}
        for info, ttl, allow_name_change in zc.registered:
            self.assertEqual(ttl, 120)
            self.assertTrue(allow_name_change)
            self.assertEqual(info.server, "ShellyPro3EM63-E08CFE96CCC4.local.")
            self.assertEqual(info.port, 80)
            self.assertEqual(info.addresses, [socket.inet_aton("192.168.30.35")])
            by_type[info.type] = info

        http = by_type["_http._tcp.local."]
        shelly = by_type["_shelly._tcp.local."]
        self.assertEqual(http.name, "shellypro3em63-e08cfe96ccc4._http._tcp.local.")
        self.assertEqual(shelly.name, "shellypro3em63-e08cfe96ccc4._shelly._tcp.local.")
        self.assertEqual(dict(http.properties), {b"gen": b"2"})
        self.assertEqual(
            dict(shelly.properties),
            {b"gen": b"2", b"app": b"Pro3EM", b"ver": b"2.0.0"},
        )

        status = mdns.status()
        self.assertTrue(status["running"])
        self.assertEqual(status["advertise_ip"], "192.168.30.35")
        self.assertCountEqual(
            status["services"],
            [http.name, shelly.name],
        )
        self.assertIsNone(status["last_error"])

    def test_init_failure_is_retried_and_register_failure_keeps_the_other(self) -> None:
        attempts = {"n": 0}
        created: list[_FakeZeroconf] = []

        class Flaky(_FakeZeroconf):
            def __init__(self) -> None:
                super().__init__()
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise OSError("no multicast")
                self.raise_on.add("_shelly.")
                created.append(self)

        mdns = ShellyMdns(_settings())
        with patch("app.shelly.mdns.Zeroconf", Flaky):
            with self.assertLogs("app.shelly.mdns", level="WARNING"):
                mdns.start()
            self.assertIsNone(mdns._zc)
            self.assertFalse(mdns.status()["running"])
            self.assertTrue(str(mdns.last_error).startswith("zeroconf_init:"))

            with self.assertLogs("app.shelly.mdns", level="WARNING"):
                mdns.start()

        self.assertEqual(len(created), 1)
        self.assertEqual(len(created[0].registered), 1)
        self.assertIn("_http.", created[0].registered[0][0].name)
        self.assertTrue(mdns.status()["running"])
        self.assertEqual(mdns.status()["services"], [created[0].registered[0][0].name])
        self.assertIn("_shelly.", str(mdns.last_error))

    def test_stop_clears_state_when_unregister_and_close_fail(self) -> None:
        class _Info:
            def __init__(self, name: str) -> None:
                self.name = name

        zc = _FakeZeroconf()
        zc.raise_on.update({"unregister", "close"})
        mdns = ShellyMdns(_settings())
        mdns._zc = zc
        mdns._infos = [
            _Info("shellypro3em63-e08cfe96ccc4._http._tcp.local."),
            _Info("shellypro3em63-e08cfe96ccc4._shelly._tcp.local."),
        ]

        with self.assertLogs("app.shelly.mdns", level="ERROR"):
            mdns.stop()

        self.assertIsNone(mdns._zc)
        self.assertEqual(mdns._infos, [])
        self.assertFalse(mdns.status()["running"])
        mdns.stop()


if __name__ == "__main__":
    unittest.main()
