"""Build Shelly Gen2 Pro 3EM-3CT63 response payloads from meter state.

Shapes are aligned to a real SPEM-003CEBEU63 (fw 2.0.0) capture.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from app.config import Settings
from app.state.energy import MeterState

_LIST_METHODS_PATH = Path(__file__).with_name("list_methods.json")
_CFG_REV = 8


def _local_now(settings: Settings) -> Tuple[datetime, int]:
    try:
        tz = ZoneInfo(settings.shelly_tz)
    except Exception:  # noqa: BLE001
        tz = timezone.utc
    now = datetime.now(tz)
    offset = int(now.utcoffset().total_seconds()) if now.utcoffset() else 0
    return now, offset


def _ipv6_link_local(mac_no_colons: str) -> str:
    """EUI-64 link-local IPv6 from MAC (matches Shelly eth.ip6 style)."""
    mac = mac_no_colons.replace(":", "").lower()
    if len(mac) != 12:
        return "fe80::"
    b = [int(mac[i : i + 2], 16) for i in range(0, 12, 2)]
    b[0] ^= 0x02
    eui = b[:3] + [0xFF, 0xFE] + b[3:]
    parts = [
        f"{(eui[0] << 8) | eui[1]:x}",
        f"{(eui[2] << 8) | eui[3]:x}",
        f"{(eui[4] << 8) | eui[5]:x}",
        f"{(eui[6] << 8) | eui[7]:x}",
    ]
    return "fe80::" + ":".join(parts)


def _load_list_methods() -> List[str]:
    try:
        data = json.loads(_LIST_METHODS_PATH.read_text(encoding="utf-8"))
        methods = data.get("methods")
        if isinstance(methods, list) and methods:
            return [str(m) for m in methods]
    except Exception:  # noqa: BLE001
        pass
    return [
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
    ]


SUPPORTED_METHODS: List[str] = _load_list_methods()


def _r(value: float, digits: int = 3) -> float:
    return round(float(value), digits)


def _r2(value: float) -> float:
    return round(float(value), 2)


def _phase_alarms() -> Dict[str, Any]:
    empty = [None, None]
    phase = {"voltage": empty, "current": empty, "power": empty}
    return {"a": dict(phase), "b": dict(phase), "c": dict(phase)}


def build_em_status(state: MeterState) -> Dict[str, Any]:
    total_current = state.a_current + state.b_current + state.c_current
    payload: Dict[str, Any] = {
        "id": 0,
        "a_current": _r(state.a_current, 3),
        "a_voltage": _r(state.a_voltage, 1),
        "a_act_power": _r(state.a_act_power, 1),
        "a_aprt_power": _r(state.a_aprt_power, 1),
        "a_pf": _r(state.a_pf, 2),
        "a_freq": _r(state.a_freq, 1),
        "b_current": _r(state.b_current, 3),
        "b_voltage": _r(state.b_voltage, 1),
        "b_act_power": _r(state.b_act_power, 1),
        "b_aprt_power": _r(state.b_aprt_power, 1),
        "b_pf": _r(state.b_pf, 2),
        "b_freq": _r(state.b_freq, 1),
        "c_current": _r(state.c_current, 3),
        "c_voltage": _r(state.c_voltage, 1),
        "c_act_power": _r(state.c_act_power, 1),
        "c_aprt_power": _r(state.c_aprt_power, 1),
        "c_pf": _r(state.c_pf, 2),
        "c_freq": _r(state.c_freq, 1),
        "n_current": None if state.n_current is None else _r(state.n_current, 3),
        "total_current": _r(total_current, 3),
        "total_act_power": _r(state.total_act_power, 3),
        "total_aprt_power": _r(state.total_aprt_power, 3),
        "user_calibrated_phase": [],
    }
    if not state.alfen_ok and state.alfen_errors:
        payload["errors"] = list(state.alfen_errors)[:3]
    return payload


def build_emdata_status(state: MeterState) -> Dict[str, Any]:
    e = state.energy
    return {
        "id": 0,
        "a_total_act_energy": _r2(e.a_total_act_energy),
        "a_total_act_ret_energy": _r2(e.a_total_act_ret_energy),
        "b_total_act_energy": _r2(e.b_total_act_energy),
        "b_total_act_ret_energy": _r2(e.b_total_act_ret_energy),
        "c_total_act_energy": _r2(e.c_total_act_energy),
        "c_total_act_ret_energy": _r2(e.c_total_act_ret_energy),
        "total_act": _r2(e.total_act),
        "total_act_ret": _r2(e.total_act_ret),
    }


def build_em_config() -> Dict[str, Any]:
    return {
        "id": 0,
        "name": None,
        "blink_mode_selector": "active_energy",
        "phase_selector": "all",
        "monitor_phase_sequence": False,
        "ct_type": "3x63A",
        "reverse": {},
        "alarms": _phase_alarms(),
    }


def build_sys_status(settings: Settings, state: MeterState) -> Dict[str, Any]:
    now_ts = time.time()
    uptime = int(now_ts - state.start_ts)
    local, utc_offset = _local_now(settings)
    return {
        "mac": settings.mac_no_colons,
        "restart_required": False,
        "time": local.strftime("%H:%M"),
        "unixtime": int(now_ts),
        "last_sync_ts": int(state.last_update_ts) if state.last_update_ts else int(now_ts),
        "uptime": uptime,
        "ram_size": 262560,
        "ram_free": 122376,
        "ram_min_free": 85852,
        "fs_size": 524288,
        "fs_free": 155648,
        "cfg_rev": _CFG_REV,
        "kvs_rev": 0,
        "schedule_rev": 0,
        "webhook_rev": 0,
        "btrelay_rev": 0,
        "available_updates": {},
        "alt": {
            "Pro3EMProAddon": {
                "name": "Shelly Pro 3 EM",
                "desc": "Pro 3 EM with Pro Sensor Addon",
                "stable": {
                    "version": settings.shelly_firmware,
                    "build_id": "20260710-101208/2.0.0-g87fbfa4",
                },
            }
        },
        "reset_reason": 3,
        "utc_offset": utc_offset,
    }


def build_wifi_status(settings: Settings) -> Dict[str, Any]:
    # Match working eth-only Pro 3EM-3CT63 (192.168.30.35): WiFi stays disconnected.
    _ = settings
    return {
        "sta_ip": "0.0.0.0",
        "status": "disconnected",
        "ssid": None,
        "sta_ip6": None,
    }


def build_eth_status(settings: Settings) -> Dict[str, Any]:
    return {
        "ip": settings.advertise_ip,
        "ip6": [_ipv6_link_local(settings.mac_no_colons)],
    }


def build_temperature_status() -> Dict[str, Any]:
    return {"id": 0, "tC": 45.0, "tF": 113.0}


def build_cloud_status() -> Dict[str, Any]:
    return {"connected": False}


def build_mqtt_status() -> Dict[str, Any]:
    return {"connected": False}


def build_ws_status() -> Dict[str, Any]:
    return {"connected": False}


def build_ble_status() -> Dict[str, Any]:
    return {}


def build_modbus_status() -> Dict[str, Any]:
    return {}


def build_full_status(settings: Settings, state: MeterState) -> Dict[str, Any]:
    return {
        "ble": build_ble_status(),
        "bthome": {},
        "cloud": build_cloud_status(),
        "em:0": build_em_status(state),
        "emdata:0": build_emdata_status(state),
        "eth": build_eth_status(settings),
        "modbus": build_modbus_status(),
        "mqtt": build_mqtt_status(),
        "sys": build_sys_status(settings, state),
        "temperature:0": build_temperature_status(),
        "wifi": build_wifi_status(settings),
        "ws": build_ws_status(),
    }


def build_device_info(settings: Settings) -> Dict[str, Any]:
    return {
        "name": None,
        "id": settings.device_hostname,
        "mac": settings.mac_no_colons,
        "slot": 1,
        "model": settings.shelly_model,
        "gen": 2,
        "fw_id": settings.shelly_fw_id,
        "ver": settings.shelly_firmware,
        "app": settings.shelly_app,
        "auth_en": False,
        "auth_domain": None,
        "profile": "triphase",
        "provision": "complete",
        "enhanced_security": False,
    }


def build_shelly_http_info(settings: Settings, state: MeterState) -> Dict[str, Any]:
    # GET /shelly matches Shelly.GetDeviceInfo on fw 2.0.0 Pro 3EM-3CT63.
    _ = state
    return build_device_info(settings)


def build_get_config(settings: Settings) -> Dict[str, Any]:
    mac = settings.mac_no_colons
    host = settings.device_hostname
    return {
        "ble": {"rpc": {"enable": False}},
        "bthome": {},
        "cloud": {"enable": False, "server": "iot.shelly.cloud:6012/jrpc"},
        "em:0": build_em_config(),
        "emdata:0": {},
        "eth": {
            "enable": True,
            "server_mode": False,
            "ipv4mode": "dhcp",
            "ip": None,
            "netmask": None,
            "gw": None,
            "nameserver": None,
        },
        "modbus": {"enable": True},
        "mqtt": {
            "enable": False,
            "server": None,
            "client_id": host,
            "user": None,
            "ssl_ca": None,
            "topic_prefix": host,
            "rpc_ntf": True,
            "status_ntf": False,
            "use_client_cert": False,
            "enable_rpc": True,
            "enable_control": True,
        },
        "sys": {
            "device": {
                "name": None,
                "mac": mac,
                "fw_id": settings.shelly_fw_id,
                "discoverable": True,
                "eco_mode": False,
                "profile": "triphase",
                "addon_type": None,
                "sys_btn_toggle": True,
                "tls_check_cert_validity_time": True,
                "enhanced_security": False,
            },
            "location": {
                "tz": settings.shelly_tz,
                "lat": settings.shelly_lat,
                "lon": settings.shelly_lon,
            },
            "debug": {
                "level": 2,
                "file_level": None,
                "mqtt": {"enable": False},
                "websocket": {"enable": False},
                "file_log": {"enable": False},
                "udp": {"addr": None},
            },
            "ui_data": {"device_revision": f"1-{_CFG_REV}"},
            "rpc_udp": {"dst_addr": None, "listen_port": None},
            "sntp": {"server": "time.cloudflare.com"},
            "cfg_rev": _CFG_REV,
        },
        "temperature:0": {
            "id": 0,
            "name": None,
            "report_thr_C": 5.0,
            "offset_C": 0.0,
        },
        "wifi": {
            "ap": {
                "ssid": f"ShellyPro3EM63-{mac}",
                "is_open": True,
                "enable": False,
                "range_extender": {"enable": False},
            },
            "sta": {
                "ssid": settings.shelly_wifi_ssid or None,
                "is_open": True,
                "enable": False,
                "ipv4mode": "dhcp",
                "ip": None,
                "netmask": None,
                "gw": None,
                "nameserver": None,
            },
            "sta1": {
                "ssid": None,
                "is_open": True,
                "enable": False,
                "ipv4mode": "dhcp",
                "ip": None,
                "netmask": None,
                "gw": None,
                "nameserver": None,
            },
            "roam": {"rssi_thr": -80, "interval": 60},
        },
        "ws": {"enable": False, "server": None, "ssl_ca": "ca.pem"},
    }


def build_get_components(
    settings: Settings,
    state: MeterState,
    params: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    params = params or {}
    include = params.get("include") or []
    if isinstance(include, str):
        include = [include]
    want_status = not include or "status" in include
    want_config = "config" in include

    full_status = build_full_status(settings, state)
    full_config = build_get_config(settings)

    keys = [
        "ble",
        "bthome",
        "cloud",
        "em:0",
        "emdata:0",
        "eth",
        "modbus",
        "mqtt",
        "sys",
        "temperature:0",
        "wifi",
        "ws",
    ]
    components: List[Dict[str, Any]] = []
    for key in keys:
        item: Dict[str, Any] = {"key": key}
        if want_status:
            item["status"] = full_status.get(key, {})
        if want_config:
            item["config"] = full_config.get(key, {})
        components.append(item)

    return {"components": components, "cfg_rev": 1, "offset": 0, "total": len(components)}


def dispatch_method(
    method: str,
    settings: Settings,
    state: MeterState,
    params: Optional[Dict[str, Any]] = None,
) -> Any:
    """Dispatch a Shelly RPC method; raise KeyError for unknown methods."""
    params = params or {}
    aliases = {
        "GetDeviceInfo": "Shelly.GetDeviceInfo",
        "GetStatus": "Shelly.GetStatus",
        "GetConfig": "Shelly.GetConfig",
        "WiFi.GetStatus": "Wifi.GetStatus",
        "MQTT.GetStatus": "Mqtt.GetStatus",
        "Mqtt.GetStatus": "Mqtt.GetStatus",
        "Ws.GetStatus": "WS.GetStatus",
        "Ble.GetStatus": "BLE.GetStatus",
    }
    method = aliases.get(method, method)

    if method == "Shelly.GetDeviceInfo":
        return build_device_info(settings)
    if method == "Shelly.GetStatus":
        return build_full_status(settings, state)
    if method == "Shelly.GetConfig":
        return build_get_config(settings)
    if method == "Shelly.ListMethods":
        return {"methods": list(SUPPORTED_METHODS)}
    if method == "Shelly.GetComponents":
        return build_get_components(settings, state, params)
    if method == "EM.GetStatus":
        return build_em_status(state)
    if method == "EM.GetConfig":
        return build_em_config()
    if method == "EMData.GetStatus":
        return build_emdata_status(state)
    if method == "EMData.GetConfig":
        return {}
    if method == "Wifi.GetStatus":
        return build_wifi_status(settings)
    if method == "Sys.GetStatus":
        return build_sys_status(settings, state)
    if method == "Eth.GetStatus":
        return build_eth_status(settings)
    if method == "Cloud.GetStatus":
        return build_cloud_status()
    if method == "Mqtt.GetStatus":
        return build_mqtt_status()
    if method == "WS.GetStatus":
        return build_ws_status()
    if method == "BLE.GetStatus":
        return build_ble_status()
    if method == "Modbus.GetStatus":
        return build_modbus_status()
    if method == "Temperature.GetStatus":
        return build_temperature_status()
    raise KeyError(method)
