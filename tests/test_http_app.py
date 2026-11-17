# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""FastAPI debug server: RPC validation, query coercion, and /debug."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
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


if __name__ == "__main__":
    unittest.main()
