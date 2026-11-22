# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""FastAPI debug server: RPC validation, query coercion, lifespan, and /debug."""

from __future__ import annotations

import asyncio
import json
import logging
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import JSONResponse

import app.main as main
from app.config import Settings
from app.debug.trace import RpcTrace
from app.shelly.rpc import ShellyRpcHandler
from app.state.energy import EnergyStore


def _request(
    method: str = "GET",
    path: str = "/",
    query: str = "",
    body: bytes = b"",
    headers: dict[str, str] | None = None,
    client: tuple[str, int] | None = ("10.0.0.8", 12345),
) -> Request:
    raw_headers = [
        (key.lower().encode("latin-1"), value.encode("latin-1"))
        for key, value in (headers or {}).items()
    ]
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": query.encode("latin-1"),
        "headers": raw_headers,
        "client": client,
        "server": ("127.0.0.1", 80),
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _json(response: JSONResponse) -> Any:
    return json.loads(response.body)


class _Alfen:
    def get_status(self) -> dict[str, Any]:
        return {
            "connected": False,
            "poll_count": 3,
            "error_count": 1,
            "last_error": "connect_failed:127.0.0.1:502",
            "last_poll_ts": 0.0,
        }


class _Mdns:
    def status(self) -> dict[str, Any]:
        return {"enabled": False, "running": False, "services": [], "last_error": None}


class HttpAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(
            alfen_host="127.0.0.1",
            state_path=str(Path(self._tmp.name) / "state.json"),
            shelly_advertise_ip="192.168.9.9",
            shelly_device_id="aabbccddeeff",
            shelly_mac="aa:bb:cc:dd:ee:ff",
            http_port=8080,
            mdns_enable=False,
            shelly_modbus_enable=False,
        )
        self.store = EnergyStore(state_path=self.settings.state_path, use_alfen_energy=False)
        self.trace = RpcTrace()
        main.settings = self.settings
        main.store = self.store
        main.alfen = _Alfen()
        main.mdns = _Mdns()
        main.trace = self.trace
        main.rpc = ShellyRpcHandler(self.settings, self.store, self.trace)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_query_coercion_skips_reserved_and_blank_values(self) -> None:
        parsed = main._parse_query_params(
            {
                "method": "Shelly.GetComponents",
                "batch": "[]",
                "params": '{"include":"config","id":9}',
                "id": "7",
                "gain": "1.5",
                "offset": "-3",
                "name": "meter",
                "empty": None,
                "flag": "",
                "already": 4,
            }
        )
        self.assertEqual(parsed["include"], "config")
        self.assertEqual(parsed["id"], 7)
        self.assertEqual(parsed["gain"], 1.5)
        self.assertEqual(parsed["offset"], -3.0)
        self.assertEqual(parsed["name"], "meter")
        self.assertEqual(parsed["flag"], "")
        self.assertEqual(parsed["already"], 4)
        self.assertNotIn("method", parsed)
        self.assertNotIn("batch", parsed)
        self.assertNotIn("empty", parsed)
        self.assertEqual(main._parse_query_params({"params": "{", "id": "0"}), {"id": 0})
        self.assertEqual(main._parse_query_params({"params": "[1]", "id": "1"}), {"id": 1})
        self.assertEqual(main._client_ip(_request(client=None)), "?")
        self.assertEqual(main._client_ip(_request()), "10.0.0.8")

    def test_health_root_shelly_and_response_headers(self) -> None:
        health = _json(main.healthz())
        self.assertTrue(health["ok"])
        self.assertFalse(health["alfen_connected"])
        self.assertFalse(health["alfen_ok"])
        self.assertEqual(health["poll_count"], 3)
        self.assertEqual(health["advertise_ip"], "192.168.9.9")

        root = main.root()
        self.assertEqual(root["device"], "shellypro3em63-aabbccddeeff")
        self.assertEqual(root["model"], "SPEM-003CEBEU63")
        self.assertIn("/debug", root["endpoints"])

        info = main.shelly_info()
        self.assertEqual(info["id"], "shellypro3em63-aabbccddeeff")
        self.assertEqual(info["mac"], "AABBCCDDEEFF")
        self.assertFalse(info["auth_en"])
        self.assertEqual(self.trace.summary()["methods"]["GET /shelly"], 1)

        async def call_next(_request: Request) -> JSONResponse:
            return JSONResponse({"ok": True})

        response = asyncio.run(main.shelly_http_headers(_request(), call_next))
        self.assertEqual(response.headers["server"], "ShellyHTTP/1.0.0")
        self.assertEqual(response.headers["connection"], "close")
        self.assertNotIn("date", response.headers)

    def test_post_rpc_parse_error_invalid_request_and_batch(self) -> None:
        broken = asyncio.run(main.rpc_post(_request("POST", "/rpc", body=b"{")))
        self.assertEqual(broken.status_code, 400)
        self.assertEqual(
            _json(broken)["error"],
            {"code": -32700, "message": "Parse error"},
        )

        rejected = asyncio.run(main.rpc_post(_request("POST", "/rpc", body=b"42")))
        self.assertEqual(rejected.status_code, 400)
        body = _json(rejected)
        self.assertEqual(body["error"], {"code": -32600, "message": "Invalid Request"})
        self.assertEqual(body["src"], "shellypro3em63-aabbccddeeff")
        self.assertEqual(self.trace.summary()["total_calls"], 0)

        anonymous = asyncio.run(
            main.rpc_post(
                _request(
                    "POST",
                    "/rpc",
                    body=b'{"id":3,"method":"EM.GetConfig"}',
                    client=None,
                )
            )
        )
        self.assertEqual(anonymous.status_code, 200)
        self.assertEqual(_json(anonymous)["result"]["ct_type"], "3x63A")
        self.assertEqual(self.trace.summary()["recent"][0]["client_ip"], "?")

        payload = json.dumps(
            [{"id": 1, "src": "sigen", "method": "EM.GetConfig"}, {"id": 2}]
        ).encode()
        batch = asyncio.run(main.rpc_post(_request("POST", "/rpc", body=payload)))
        self.assertEqual(batch.status_code, 200)
        items = _json(batch)
        self.assertEqual(items[0]["id"], 1)
        self.assertEqual(items[0]["dst"], "sigen")
        self.assertEqual(items[0]["result"]["ct_type"], "3x63A")
        self.assertEqual(items[1]["error"]["code"], -32600)

    def test_get_rpc_rejects_bad_batches_and_coerces_method_paths(self) -> None:
        missing = asyncio.run(main.rpc_get(_request("GET", "/rpc")))
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(_json(missing), {"error": "method required"})

        queried = asyncio.run(
            main.rpc_get(
                _request(
                    "GET",
                    "/rpc",
                    query="method=Shelly.GetComponents&include=config&id=0",
                )
            )
        )
        self.assertEqual(queried.status_code, 200)
        queried_body = _json(queried)
        self.assertIn("config", queried_body["components"][0])
        self.assertNotIn("status", queried_body["components"][0])
        self.assertNotIn("error", queried_body)

        for query in ("batch={", "batch=%7B%7D"):
            with self.subTest(query=query):
                rejected = asyncio.run(main.rpc_get(_request("GET", "/rpc", query=query)))
                self.assertEqual(rejected.status_code, 400)
                self.assertEqual(_json(rejected), {"error": "invalid batch"})

        object_batch = asyncio.run(
            main.rpc_get(
                _request("GET", "/rpc", query=urlencode({"batch": json.dumps({"method": "EM.GetConfig"})}))
            )
        )
        self.assertEqual(object_batch.status_code, 400)
        self.assertEqual(_json(object_batch), {"error": "invalid batch"})

        batch = asyncio.run(
            main.rpc_get(
                _request(
                    "GET",
                    "/rpc",
                    query=urlencode(
                        {
                            "batch": json.dumps(
                                [
                                    {"method": "EM.GetConfig"},
                                    "skip-me",
                                    {"params": [1]},
                                    {
                                        "method": "Shelly.GetComponents",
                                        "params": {"include": "config"},
                                    },
                                ]
                            )
                        }
                    ),
                )
            )
        )
        results = _json(batch)
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["ct_type"], "3x63A")
        self.assertIn("config", results[1]["components"][0])
        self.assertNotIn("status", results[1]["components"][0])

        components = asyncio.run(
            main.rpc_get_method(
                "Shelly.GetComponents",
                _request("GET", "/rpc/Shelly.GetComponents", query="include=config&id=4"),
            )
        )
        payload = _json(components)
        self.assertIn("config", payload["components"][0])
        self.assertNotIn("status", payload["components"][0])
        self.assertNotIn("error", payload)

    def test_debug_json_and_html_show_meter_and_recent_calls(self) -> None:
        self.store.state.alfen_ok = True
        self.store.state.a_act_power = 1200.0
        self.store.state.total_act_power = 1200.0
        main.alfen = _ConnectedAlfen()
        asyncio.run(
            main.rpc_post(
                _request(
                    "POST",
                    "/rpc",
                    body=b'{"id":1,"method":"EM.GetStatus","src":"sigen"}',
                    client=("10.4.5.6", 9),
                )
            )
        )

        debug_json = main.debug_page(_request(headers={"Accept": "application/json"}))
        self.assertIsInstance(debug_json, JSONResponse)
        payload = _json(debug_json)
        self.assertEqual(payload["alfen"]["poll_count"], 3)
        self.assertEqual(payload["alfen"]["last_error"], "connect_failed:127.0.0.1:502")
        self.assertTrue(payload["alfen"]["connected"])
        self.assertEqual(payload["meter"]["power"]["total"], 1200.0)
        self.assertEqual(payload["meter"]["alfen_ok"], True)
        self.assertFalse(payload["mdns"]["running"])
        self.assertEqual(payload["rpc"]["methods"]["EM.GetStatus"], 1)
        self.assertEqual(payload["settings"]["advertise_ip"], "192.168.9.9")
        self.assertEqual(payload["settings"]["http_port"], 8080)
        self.assertEqual(payload["settings"]["device_id"], "aabbccddeeff")

        page = main.debug_page(_request())
        html = page.body.decode()
        self.assertIn("sigelly_emu /debug", html)
        self.assertIn("shellypro3em63-aabbccddeeff", html)
        self.assertIn("class=\"ok\"", html)
        self.assertIn("10.4.5.6", html)
        self.assertIn("EM.GetStatus", html)
        self.assertIn(">ok<", html)
        self.assertNotIn("no calls yet", html)

        main.alfen = _Alfen()
        self.store.state.alfen_ok = False
        self.trace = RpcTrace()
        main.trace = self.trace
        empty = main.debug_page(_request())
        self.assertIn("class=\"bad\"", empty.body.decode())
        self.assertIn("no calls yet", empty.body.decode())


class _ConnectedAlfen(_Alfen):
    def get_status(self) -> dict[str, Any]:
        status = super().get_status()
        status["connected"] = True
        return status


_MAIN_GLOBALS = ("settings", "store", "alfen", "mdns", "modbus", "trace", "rpc")


class _LifespanAlfen:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


class _LifespanModbus:
    def __init__(self, settings: Settings, store: EnergyStore, port: int, unit_id: int) -> None:
        self.settings = settings
        self.store = store
        self.port = port
        self.unit_id = unit_id

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


class _LifespanMdns:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


class DebugLifespanTests(unittest.TestCase):
    def test_unknown_log_level_falls_back_to_info(self) -> None:
        with patch("app.main.logging.basicConfig") as basic:
            main._configure_logging("not-a-level")
            main._configure_logging("warning")
        self.assertEqual(
            basic.call_args_list[0].kwargs["level"],
            logging.INFO,
        )
        self.assertEqual(basic.call_args_list[1].kwargs["level"], logging.WARNING)
        self.assertEqual(
            basic.call_args_list[0].kwargs["format"],
            "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        )

    def test_lifespan_does_not_wait_on_mdns_and_persists_energy(self) -> None:
        self._exercise(modbus_enabled=True, mdns_error=None)

    def test_lifespan_skips_modbus_and_survives_mdns_startup_failure(self) -> None:
        self._exercise(modbus_enabled=False, mdns_error=RuntimeError("probe timeout"))

    def _exercise(self, *, modbus_enabled: bool, mdns_error: BaseException | None) -> None:
        started: list[str] = []
        stopped: list[str] = []
        entered = threading.Event()
        release = threading.Event()
        threads: list[threading.Thread] = []
        real_thread = threading.Thread

        def capture_thread(*args: Any, **kwargs: Any) -> threading.Thread:
            thread = real_thread(*args, **kwargs)
            threads.append(thread)
            return thread

        class Alfen(_LifespanAlfen):
            def start(self) -> None:
                started.append("alfen")

            def stop(self) -> None:
                stopped.append("alfen")

        class Modbus(_LifespanModbus):
            def start(self) -> None:
                started.append("modbus")

            def stop(self) -> None:
                stopped.append("modbus")

        class Mdns(_LifespanMdns):
            def start(self) -> None:
                started.append("mdns")
                entered.set()
                if mdns_error is not None:
                    raise mdns_error
                release.wait()

            def stop(self) -> None:
                stopped.append("mdns")

        previous = {name: getattr(main, name) for name in _MAIN_GLOBALS if hasattr(main, name)}
        missing = [name for name in _MAIN_GLOBALS if not hasattr(main, name)]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                state_path = Path(tmp) / "state.json"
                settings = Settings(
                    alfen_host="10.1.1.1",
                    alfen_port=1502,
                    alfen_slave_id=7,
                    alfen_poll_interval=0.5,
                    alfen_connect_timeout=1.5,
                    alfen_use_energy=True,
                    state_path=str(state_path),
                    shelly_advertise_ip="192.168.9.9",
                    shelly_device_id="aabbccddeeff",
                    shelly_mac="aa:bb:cc:dd:ee:ff",
                    http_port=8080,
                    mdns_enable=False,
                    shelly_modbus_enable=modbus_enabled,
                    shelly_modbus_port=5502,
                    shelly_modbus_unit_id=7,
                    log_level="warning",
                )
                with (
                    patch("app.main.get_settings", return_value=settings),
                    patch("app.main.AlfenClient", Alfen),
                    patch("app.main.ShellyModbusServer", Modbus),
                    patch("app.main.ShellyMdns", Mdns),
                    patch("app.main.threading.Thread", capture_thread),
                    patch("app.main.logging.basicConfig") as basic,
                    self.assertLogs("app.main", level="INFO") as logs,
                ):
                    asyncio.run(
                        self._run_lifespan(
                            settings,
                            started,
                            stopped,
                            entered,
                            threads,
                            mdns_blocks=mdns_error is None,
                        )
                    )
                saved = json.loads(state_path.read_text(encoding="utf-8"))
        finally:
            release.set()
            for thread in threads:
                thread.join(2)
                self.assertFalse(thread.is_alive())
            for name in missing:
                if hasattr(main, name):
                    delattr(main, name)
            for name, value in previous.items():
                setattr(main, name, value)

        self.assertEqual(basic.call_args.kwargs["level"], logging.WARNING)
        self.assertEqual(len(threads), 1)
        self.assertEqual(threads[0].name, "mdns-start")
        self.assertTrue(threads[0].daemon)
        self.assertEqual(saved["energy"]["total_act"], 12.5)
        self.assertEqual(saved["energy"]["source"], "integrated")
        text = "\n".join(logs.output)
        self.assertIn("sigelly_emu shutdown complete", text)
        if modbus_enabled:
            self.assertEqual(started, ["alfen", "modbus", "mdns"])
            self.assertEqual(stopped, ["mdns", "modbus", "alfen"])
            self.assertIn("modbus=:5502", text)
            self.assertNotIn("probe timeout", text)
        else:
            self.assertEqual(started, ["alfen", "mdns"])
            self.assertEqual(stopped, ["mdns", "alfen"])
            self.assertIn("modbus=off", text)
            self.assertIn("Background mDNS start failed", text)
            self.assertIn("probe timeout", text)

    async def _run_lifespan(
        self,
        settings: Settings,
        started: list[str],
        stopped: list[str],
        entered: threading.Event,
        threads: list[threading.Thread],
        *,
        mdns_blocks: bool,
    ) -> None:
        async with main.lifespan(main.app):
            self.assertTrue(entered.wait(2))
            if not mdns_blocks:
                threads[0].join(2)
                self.assertFalse(threads[0].is_alive())
            self.assertIs(main.settings, settings)
            callback = main.alfen.kwargs["on_update"]
            self.assertIs(callback.__self__, main.store)
            self.assertIs(callback.__func__, EnergyStore.update_from_alfen)
            self.assertEqual(main.alfen.kwargs["host"], "10.1.1.1")
            self.assertEqual(main.alfen.kwargs["port"], 1502)
            self.assertEqual(main.alfen.kwargs["slave_id"], 7)
            self.assertEqual(main.alfen.kwargs["poll_interval"], 0.5)
            self.assertEqual(main.alfen.kwargs["connect_timeout"], 1.5)
            self.assertTrue(main.store._use_alfen_energy_flag)
            self.assertEqual(main.store.state_path, Path(settings.state_path))
            self.assertIs(main.rpc.settings, settings)
            self.assertIs(main.rpc.store, main.store)
            self.assertIs(main.rpc.trace, main.trace)
            if settings.shelly_modbus_enable:
                self.assertEqual(main.modbus.port, 5502)
                self.assertEqual(main.modbus.unit_id, 7)
                self.assertIs(main.modbus.store, main.store)
                self.assertEqual(started, ["alfen", "modbus", "mdns"])
            else:
                self.assertEqual(started, ["alfen", "mdns"])
            self.assertEqual(threads[0].name, "mdns-start")
            self.assertEqual(threads[0].is_alive(), mdns_blocks)
            main.store.state.energy.total_act = 12.5
        self.assertEqual(stopped[0], "mdns")
        self.assertEqual(threads[0].is_alive(), mdns_blocks)


if __name__ == "__main__":
    unittest.main()
