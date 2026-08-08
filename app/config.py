"""Environment-driven configuration for sigelly_emu."""

from __future__ import annotations

import socket
from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def detect_lan_ip() -> str:
    """Best-effort LAN IP via UDP connect (does not send packets)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Alfen Modbus source
    alfen_host: str = Field(..., description="Alfen Eve Pro Single IP or hostname")
    alfen_port: int = 502
    alfen_slave_id: int = 1
    alfen_poll_interval: float = 2.0
    alfen_connect_timeout: float = 3.0

    # Shelly identity
    shelly_device_id: str = "349454112233"
    shelly_mac: str = "34:94:54:11:22:33"
    shelly_model: str = "SHPRO-3EM-3CT63"
    shelly_firmware: str = "1.4.4"
    shelly_fw_id: str = "20241001-000000/v1.4.4@emu"
    shelly_app: str = "Pro3EM"
    shelly_sn: str = "EMU000001"
    shelly_wifi_ssid: str = "home"

    # Network / service
    http_port: int = 80
    shelly_advertise_ip: Optional[str] = None
    mdns_enable: bool = True
    state_path: str = "/data/state.json"
    log_level: str = "INFO"

    @property
    def advertise_ip(self) -> str:
        if self.shelly_advertise_ip:
            return self.shelly_advertise_ip
        return detect_lan_ip()

    @property
    def device_hostname(self) -> str:
        return f"shellypro3em-{self.shelly_device_id}"

    @property
    def mac_no_colons(self) -> str:
        return self.shelly_mac.replace(":", "").upper()


@lru_cache
def get_settings() -> Settings:
    return Settings()
