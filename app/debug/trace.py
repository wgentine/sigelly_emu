"""RPC access tracing for /debug and pairing diagnostics."""

from __future__ import annotations

import threading
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass
from typing import Any, Deque, Dict, List, Optional


@dataclass
class RpcEvent:
    ts: float
    client_ip: str
    method: str
    transport: str
    ok: bool


class RpcTrace:
    def __init__(self, maxlen: int = 200) -> None:
        self._lock = threading.Lock()
        self._events: Deque[RpcEvent] = deque(maxlen=maxlen)
        self._methods: Counter = Counter()
        self._errors: Counter = Counter()

    def record(self, client_ip: str, method: str, transport: str, ok: bool) -> None:
        with self._lock:
            self._events.appendleft(
                RpcEvent(
                    ts=time.time(),
                    client_ip=client_ip,
                    method=method,
                    transport=transport,
                    ok=ok,
                )
            )
            self._methods[method] += 1
            if not ok:
                self._errors[method] += 1

    def recent(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._lock:
            return [asdict(e) for e in list(self._events)[:limit]]

    def summary(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "total_calls": sum(self._methods.values()),
                "methods": dict(self._methods),
                "errors": dict(self._errors),
                "recent": [asdict(e) for e in list(self._events)[:30]],
            }
