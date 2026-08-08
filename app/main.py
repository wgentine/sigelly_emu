"""Shelly Pro 3EM-3CT63 emulator backed by Alfen Eve Pro Modbus TCP."""

from __future__ import annotations

import json
import logging
import threading
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.alfen.client import AlfenClient
from app.config import Settings, get_settings
from app.debug.trace import RpcTrace
from app.shelly.mdns import ShellyMdns
from app.shelly.rpc import ShellyRpcHandler
from app.shelly.responses import build_shelly_http_info
from app.state.energy import EnergyStore

logger = logging.getLogger(__name__)

# Shared runtime objects (set in lifespan)
settings: Settings
store: EnergyStore
alfen: AlfenClient
mdns: ShellyMdns
trace: RpcTrace
rpc: ShellyRpcHandler


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def _start_mdns_async(mdns_svc: ShellyMdns) -> None:
    """Register mDNS off the ASGI lifespan path so probe timeouts cannot block startup."""
    try:
        mdns_svc.start()
    except Exception:  # noqa: BLE001
        logger.exception("Background mDNS start failed")


def _client_ip(request: Request) -> str:
    if request.client:
        return request.client.host
    return "?"


def _parse_query_params(qp: Dict[str, Any]) -> Dict[str, Any]:
    params: Dict[str, Any] = {}
    if "params" in qp:
        try:
            parsed = json.loads(qp["params"])
            if isinstance(parsed, dict):
                params.update(parsed)
        except Exception:  # noqa: BLE001
            pass
    for key, value in qp.items():
        if key in ("method", "params", "batch"):
            continue
        if value is None:
            continue
        if isinstance(value, str) and value.isdigit():
            params[key] = int(value)
        else:
            try:
                params[key] = float(value) if isinstance(value, str) else value
            except Exception:  # noqa: BLE001
                params[key] = value
    return params


@asynccontextmanager
async def lifespan(app: FastAPI):
    global settings, store, alfen, mdns, trace, rpc

    settings = get_settings()
    _configure_logging(settings.log_level)

    store = EnergyStore(
        state_path=settings.state_path,
        use_alfen_energy=settings.alfen_use_energy,
    )
    trace = RpcTrace()
    rpc = ShellyRpcHandler(settings, store, trace)

    alfen = AlfenClient(
        host=settings.alfen_host,
        port=settings.alfen_port,
        slave_id=settings.alfen_slave_id,
        poll_interval=settings.alfen_poll_interval,
        connect_timeout=settings.alfen_connect_timeout,
        on_update=store.update_from_alfen,
    )
    alfen.start()

    mdns = ShellyMdns(settings)
    threading.Thread(target=_start_mdns_async, args=(mdns,), name="mdns-start", daemon=True).start()

    logger.info(
        "sigelly_emu ready advertise=%s:%s device=%s alfen=%s:%s",
        settings.advertise_ip,
        settings.http_port,
        settings.device_hostname,
        settings.alfen_host,
        settings.alfen_port,
    )
    yield

    mdns.stop()
    alfen.stop()
    store.save()
    logger.info("sigelly_emu shutdown complete")


app = FastAPI(title="sigelly_emu", docs_url=None, redoc_url=None, lifespan=lifespan)


@app.middleware("http")
async def shelly_http_headers(request: Request, call_next):
    """Match real Gen2 firmware response headers (Sigen fingerprints these)."""
    response = await call_next(request)
    response.headers["Server"] = "ShellyHTTP/1.0.0"
    response.headers["Connection"] = "close"
    # Real ShellyHTTP omits Date; drop uvicorn/Starlette's if present.
    if "date" in response.headers:
        del response.headers["date"]
    return response


@app.get("/")
def root() -> Dict[str, Any]:
    return {
        "service": "sigelly_emu",
        "device": settings.device_hostname,
        "model": settings.shelly_model,
        "endpoints": ["/shelly", "/rpc", "/healthz", "/debug"],
    }


@app.get("/healthz")
def healthz() -> JSONResponse:
    alfen_status = alfen.get_status()
    payload = {
        "ok": True,
        "alfen_connected": alfen_status["connected"],
        "alfen_ok": store.state.alfen_ok,
        "poll_count": alfen_status["poll_count"],
        "advertise_ip": settings.advertise_ip,
    }
    return JSONResponse(payload)


@app.get("/shelly")
def shelly_info() -> Dict[str, Any]:
    trace.record("?", "GET /shelly", "GET", ok=True)
    return build_shelly_http_info(settings, store.snapshot())


@app.post("/rpc")
async def rpc_post(request: Request) -> JSONResponse:
    client = _client_ip(request)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse(
            {
                "id": None,
                "src": settings.device_hostname,
                "error": {"code": -32700, "message": "Parse error"},
            },
            status_code=400,
        )

    if isinstance(body, list):
        results = rpc.handle_batch(body, client_ip=client, transport="POST")
        return JSONResponse(results)

    if not isinstance(body, dict):
        return JSONResponse(
            {
                "id": None,
                "src": settings.device_hostname,
                "error": {"code": -32600, "message": "Invalid Request"},
            },
            status_code=400,
        )

    return JSONResponse(rpc.handle_call(body, client_ip=client, transport="POST"))


@app.get("/rpc")
async def rpc_get(request: Request) -> JSONResponse:
    client = _client_ip(request)
    qp = dict(request.query_params)

    if "batch" in qp:
        try:
            batch = json.loads(qp["batch"])
            if not isinstance(batch, list):
                raise ValueError("batch must be a list")
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "invalid batch"}, status_code=400)
        results = []
        for item in batch:
            if not isinstance(item, dict):
                continue
            method = item.get("method")
            params = item.get("params") or {}
            if not method:
                continue
            results.append(
                rpc.handle_get_result(str(method), params if isinstance(params, dict) else {}, client)
            )
        return JSONResponse(results)

    method = qp.get("method")
    if not method:
        return JSONResponse({"error": "method required"}, status_code=400)
    params = _parse_query_params(qp)
    return JSONResponse(rpc.handle_get_result(method, params, client))


@app.get("/rpc/{method}")
async def rpc_get_method(method: str, request: Request) -> JSONResponse:
    client = _client_ip(request)
    qp = dict(request.query_params)
    params = _parse_query_params(qp)
    return JSONResponse(rpc.handle_get_result(method, params, client))


@app.get("/debug")
def debug_page(request: Request):
    """HTML dashboard for pairing diagnostics (JSON if Accept: application/json)."""
    meter = store.debug_dict()
    mdns_status = mdns.status()
    rpc_summary = trace.summary()
    alfen_status = alfen.get_status()
    alfen_info = {
        "host": settings.alfen_host,
        "port": settings.alfen_port,
        "slave_id": settings.alfen_slave_id,
        "connected": alfen_status["connected"],
        "poll_count": alfen_status["poll_count"],
        "error_count": alfen_status["error_count"],
        "last_error": alfen_status["last_error"],
        "last_poll_ts": alfen_status["last_poll_ts"],
    }

    accept = request.headers.get("accept", "")
    if "application/json" in accept:
        return JSONResponse(
            {
                "alfen": alfen_info,
                "meter": meter,
                "mdns": mdns_status,
                "rpc": rpc_summary,
                "settings": {
                    "device_id": settings.shelly_device_id,
                    "model": settings.shelly_model,
                    "advertise_ip": settings.advertise_ip,
                    "http_port": settings.http_port,
                },
            }
        )

    recent_rows = "".join(
        f"<tr><td>{e['ts']:.0f}</td><td>{e['client_ip']}</td>"
        f"<td>{e['transport']}</td><td>{e['method']}</td>"
        f"<td>{'ok' if e['ok'] else 'ERR'}</td></tr>"
        for e in rpc_summary.get("recent", [])
    )
    alfen_ok_class = "ok" if alfen_status["connected"] and meter.get("alfen_ok") else "bad"
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>sigelly_emu debug</title>
<meta http-equiv="refresh" content="5">
<style>
body {{ font-family: ui-monospace, monospace; margin: 1.5rem; background:#111; color:#ddd; }}
h1,h2 {{ color:#9cf; }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 1.5rem; }}
td,th {{ border: 1px solid #444; padding: 0.35rem 0.6rem; text-align: left; }}
th {{ background:#222; }}
.ok {{ color:#6c6; }} .bad {{ color:#f66; }}
pre {{ background:#1a1a1a; padding: 0.8rem; overflow:auto; }}
</style></head><body>
<h1>sigelly_emu /debug</h1>
<p>Device: <b>{settings.device_hostname}</b> · Model: {settings.shelly_model}
· Advertise: {settings.advertise_ip}:{settings.http_port}</p>
<h2>Alfen Modbus</h2>
<pre>{json.dumps(alfen_info, indent=2)}</pre>
<p class="{alfen_ok_class}">
Alfen connected={alfen_status["connected"]} last_ok={meter.get('alfen_ok')}
</p>
<h2>Live power (W)</h2>
<pre>{json.dumps(meter.get('power'), indent=2)}</pre>
<h2>mDNS</h2>
<pre>{json.dumps(mdns_status, indent=2)}</pre>
<h2>Recent RPC calls</h2>
<table>
<tr><th>ts</th><th>client</th><th>transport</th><th>method</th><th>status</th></tr>
{recent_rows or '<tr><td colspan="5">no calls yet</td></tr>'}
</table>
<p>JSON: <code>curl -H 'Accept: application/json' http://{settings.advertise_ip}:{settings.http_port}/debug</code></p>
</body></html>"""
    return HTMLResponse(html)
