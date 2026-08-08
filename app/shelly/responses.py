"""Build Shelly Gen2 Pro 3EM-3CT63 response payloads from meter state."""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from app.config import Settings
from app.state.energy import MeterState


SUPPORTED_METHODS: List[str] = [
    "Shelly.GetDeviceInfo",
    "Shelly.GetStatus",
    "Shelly.GetConfig",
    "Shelly.ListMethods",
    "EM.GetStatus",
    "EM.GetConfig",
    "EMData.GetStatus",
    "EMData.GetConfig",
    "Wifi.GetStatus",
    "Sys.GetStatus",
]


def _r(value: float, digits: int = 3) -> float:
    return round(float(value), digits)


def _r2(value: float) -> float:
    return round(float(value), 2)


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


def build_sys_status(settings: Settings, state: MeterState) -> Dict[str, Any]:
    now = time.time()
    uptime = int(now - state.start_ts)
    local = time.localtime(now)
    return {
        "mac": settings.mac_no_colons,
        "restart_required": False,
        "time": time.strftime("%H:%M", local),
        "unixtime": int(now),
        "last_sync_ts": int(state.last_update_ts) if state.last_update_ts else int(now),
        "uptime": uptime,
        "ram_size": 247868,
        "ram_free": 126504,
        "ram_min_free": 108024,
        "fs_size": 524288,
        "fs_free": 188416,
        "cfg_rev": 1,
        "kvs_rev": 0,
        "schedule_rev": 0,
        "webhook_rev": 0,
        "available_updates": {},
        "reset_reason": 1,
    }


def build_wifi_status(settings: Settings) -> Dict[str, Any]:
    return {
        "sta_ip": settings.advertise_ip,
        "status": "got ip",
        "ssid": settings.shelly_wifi_ssid,
        "rssi": -50,
    }


def build_full_status(settings: Settings, state: MeterState) -> Dict[str, Any]:
    return {
        "ble": {},
        "bthome": {"errors": ["bluetooth_disabled"]},
        "cloud": {"connected": False},
        "em:0": build_em_status(state),
        "emdata:0": build_emdata_status(state),
        "eth": {"ip": settings.advertise_ip},
        "modbus": {},
        "mqtt": {"connected": False},
        "sys": build_sys_status(settings, state),
        "temperature:0": {"id": 0, "tC": 35.0, "tF": 95.0},
        "wifi": build_wifi_status(settings),
        "ws": {"connected": False},
    }


def build_device_info(settings: Settings) -> Dict[str, Any]:
    return {
        "id": settings.device_hostname,
        "mac": settings.mac_no_colons,
        "model": settings.shelly_model,
        "gen": 2,
        "fw_id": settings.shelly_fw_id,
        "ver": settings.shelly_firmware,
        "app": settings.shelly_app,
        "auth_en": False,
        "auth_domain": None,
        "discoverable": True,
    }


def build_shelly_http_info(settings: Settings, state: MeterState) -> Dict[str, Any]:
    info = build_device_info(settings)
    info.update(
        {
            "name": settings.device_hostname,
            "sn": settings.shelly_sn,
            "manufacturer": "Allterco Robotics",
            "uptime": int(time.time() - state.start_ts),
            "wifi_sta": {"connected": True},
            "eth": {"connected": True},
        }
    )
    return info


def build_get_config(settings: Settings) -> Dict[str, Any]:
    return {
        "ble": {"enable": False},
        "bthome": {},
        "cloud": {"enable": False, "server": "shelly-eu-2.shelly.cloud:6022/jrpc"},
        "em:0": {
            "id": 0,
            "name": None,
            "blink_mode_selector": "active_energy",
            "phase_selector": "a",
            "monitor_phase_sequence": True,
            "ct_range": "63A",
        },
        "emdata:0": {},
        "eth": {"enable": True},
        "modbus": {"enable": False},
        "mqtt": {"enable": False},
        "sys": {
            "device": {
                "name": settings.device_hostname,
                "mac": settings.mac_no_colons,
                "fw_id": settings.shelly_fw_id,
                "discoverable": True,
                "eco_mode": False,
            },
            "location": {"tz": None, "lat": None, "lon": None},
            "debug": {"mqtt": {"enable": False}, "websocket": {"enable": False}, "udp": None},
            "ui_data": {},
            "rpc_udp": {"dst_addr": None, "listen_port": None},
            "sntp": {"server": "time.google.com"},
            "cfg_rev": 1,
        },
        "wifi": {
            "ap": {"ssid": f"{settings.device_hostname}-ap", "is_open": True, "enable": False},
            "sta": {
                "ssid": settings.shelly_wifi_ssid,
                "is_open": False,
                "enable": True,
                "ipv4mode": "dhcp",
                "ip": settings.advertise_ip,
            },
            "sta1": {"ssid": None, "is_open": True, "enable": False},
        },
        "ws": {"enable": False, "server": None, "ssl_ca": "*"},
    }


def build_em_config() -> Dict[str, Any]:
    return {
        "id": 0,
        "name": None,
        "blink_mode_selector": "active_energy",
        "phase_selector": "a",
        "monitor_phase_sequence": True,
        "ct_range": "63A",
        "ct_type": [63, 63, 63],
    }


def dispatch_method(
    method: str,
    settings: Settings,
    state: MeterState,
    params: Optional[Dict[str, Any]] = None,
) -> Any:
    """Dispatch a Shelly RPC method; raise KeyError for unknown methods."""
    _ = params  # currently unused; reserved for id filtering
    if method in ("Shelly.GetDeviceInfo", "GetDeviceInfo"):
        return build_device_info(settings)
    if method in ("Shelly.GetStatus", "GetStatus"):
        return build_full_status(settings, state)
    if method in ("Shelly.GetConfig", "GetConfig"):
        return build_get_config(settings)
    if method == "Shelly.ListMethods":
        return {"methods": list(SUPPORTED_METHODS)}
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
    raise KeyError(method)
