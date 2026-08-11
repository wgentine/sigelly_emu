# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""Shelly Gen2 JSON-RPC request handling (POST envelope + GET bare result)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Union

from app.config import Settings
from app.debug.trace import RpcTrace
from app.shelly.responses import dispatch_method
from app.state.energy import EnergyStore

logger = logging.getLogger(__name__)


class ShellyRpcHandler:
    def __init__(
        self,
        settings: Settings,
        store: EnergyStore,
        trace: RpcTrace,
    ) -> None:
        self.settings = settings
        self.store = store
        self.trace = trace

    def handle_call(
        self,
        body: Dict[str, Any],
        client_ip: Optional[str] = None,
        transport: str = "POST",
    ) -> Dict[str, Any]:
        """Handle one JSON-RPC-style object; always return an envelope dict."""
        req_id = body.get("id")
        method = body.get("method")
        params = body.get("params") or {}
        if not isinstance(params, dict):
            params = {}

        src = self.settings.device_hostname
        dst = body.get("src", "unknown")

        if not method or not isinstance(method, str):
            self.trace.record(client_ip or "?", "<missing>", transport, ok=False)
            return {
                "id": req_id,
                "src": src,
                "dst": dst,
                "error": {"code": -32600, "message": "Invalid Request"},
            }

        try:
            state = self.store.snapshot()
            result = dispatch_method(method, self.settings, state, params)
            self.trace.record(client_ip or "?", method, transport, ok=True)
            return {
                "id": req_id,
                "src": src,
                "dst": dst,
                "result": result,
            }
        except KeyError:
            self.trace.record(client_ip or "?", method, transport, ok=False)
            logger.info("Unknown RPC method from %s: %s", client_ip, method)
            return {
                "id": req_id,
                "src": src,
                "dst": dst,
                "error": {"code": 404, "message": "Method not found"},
            }
        except Exception as exc:  # noqa: BLE001
            self.trace.record(client_ip or "?", method, transport, ok=False)
            logger.exception("RPC handler error for %s", method)
            return {
                "id": req_id,
                "src": src,
                "dst": dst,
                "error": {"code": -32000, "message": str(exc)},
            }

    def handle_batch(
        self,
        items: List[Dict[str, Any]],
        client_ip: Optional[str] = None,
        transport: str = "POST",
    ) -> List[Dict[str, Any]]:
        return [self.handle_call(item, client_ip=client_ip, transport=transport) for item in items]

    def handle_get_result(
        self,
        method: str,
        params: Dict[str, Any],
        client_ip: Optional[str] = None,
    ) -> Union[Dict[str, Any], Any]:
        """GET semantics: return bare result or error object."""
        envelope = self.handle_call(
            {"id": None, "method": method, "params": params},
            client_ip=client_ip,
            transport="GET",
        )
        if "result" in envelope:
            return envelope["result"]
        return envelope
