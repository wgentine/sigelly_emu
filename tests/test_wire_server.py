# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 W. Gentine
"""Shelly-accurate HTTP response formatting."""

from __future__ import annotations

import unittest

from app.wire_server import _shelly_http_response


class WireServerResponseTests(unittest.TestCase):
    def test_title_case_headers_and_no_date(self) -> None:
        body = b'{"ok":true}'
        raw = _shelly_http_response(body)
        text = raw.decode("ascii")
        self.assertIn("Server: ShellyHTTP/1.0.0", text)
        self.assertIn("Connection: close", text)
        self.assertIn("Content-Type: application/json", text)
        self.assertNotIn("Date:", text)
        self.assertTrue(text.endswith('{"ok":true}'))

    def test_pragma_on_shelly_route(self) -> None:
        body = b"{}"
        raw = _shelly_http_response(body, pragma_no_cache=True)
        self.assertIn("Pragma: no-cache", raw.decode("ascii"))


if __name__ == "__main__":
    unittest.main()
