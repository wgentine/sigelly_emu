# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""mDNS advertisement for Shelly Gen2 discovery (_http._tcp and _shelly._tcp)."""

from __future__ import annotations

import logging
import socket
from typing import List, Optional

from zeroconf import ServiceInfo, Zeroconf

from app.config import Settings

logger = logging.getLogger(__name__)


class ShellyMdns:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._zc: Optional[Zeroconf] = None
        self._infos: List[ServiceInfo] = []
        self.last_error: Optional[str] = None

    def _properties_http(self) -> dict:
        # Real _http._tcp TXT is only gen=2
        return {b"gen": b"2"}

    def _properties_shelly(self) -> dict:
        # Real _shelly._tcp TXT: gen / app / ver only
        return {
            b"gen": b"2",
            b"app": self.settings.shelly_app.encode(),
            b"ver": self.settings.shelly_firmware.encode(),
        }

    def _make_info(self, service_type: str) -> ServiceInfo:
        instance = self.settings.device_hostname
        props = (
            self._properties_http()
            if service_type.startswith("_http.")
            else self._properties_shelly()
        )
        return ServiceInfo(
            service_type,
            f"{instance}.{service_type}",
            addresses=[socket.inet_aton(self.settings.advertise_ip)],
            port=self.settings.http_port,
            properties=props,
            server=f"{self.settings.mdns_server_hostname}.local.",
        )

    def start(self) -> None:
        if not self.settings.mdns_enable:
            logger.info("mDNS disabled via MDNS_ENABLE=false")
            return
        if self._zc is not None:
            return

        try:
            self._zc = Zeroconf()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"zeroconf_init:{exc}"
            logger.warning("mDNS init failed (continuing without mDNS): %s", exc)
            self._zc = None
            return

        infos = [
            self._make_info("_http._tcp.local."),
            self._make_info("_shelly._tcp.local."),
        ]
        registered: List[ServiceInfo] = []
        for info in infos:
            try:
                # allow_name_change avoids long probe timeouts on crowded LANs
                self._zc.register_service(info, ttl=120, allow_name_change=True)
                registered.append(info)
                logger.info(
                    "mDNS registered %s -> %s:%s",
                    info.name,
                    self.settings.advertise_ip,
                    self.settings.http_port,
                )
            except Exception as exc:  # noqa: BLE001
                self.last_error = f"register:{info.name}:{exc}"
                logger.warning(
                    "mDNS register failed for %s (continuing): %s",
                    info.name,
                    exc,
                )
        self._infos = registered
        if not registered:
            logger.warning("No mDNS services registered; HTTP RPC still available")

    def stop(self) -> None:
        if not self._zc:
            return
        for info in self._infos:
            try:
                self._zc.unregister_service(info)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to unregister %s", info.name)
        try:
            self._zc.close()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to close Zeroconf")
        self._zc = None
        self._infos = []
        logger.info("mDNS stopped")

    def status(self) -> dict:
        return {
            "enabled": self.settings.mdns_enable,
            "running": self._zc is not None,
            "advertise_ip": self.settings.advertise_ip,
            "port": self.settings.http_port,
            "services": [i.name for i in self._infos],
            "last_error": self.last_error,
        }
