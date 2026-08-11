"""Test seam for the Client's HTTP transport.

Tests historically patched module-level ``requests.request``. The client now
sends everything through a keep-alive ``requests.Session`` inside
``Client._http_request``, so that is the seam to patch — fakes keep their
``(method, url, **kwargs)`` signature and still receive the full URL.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest


def patch_client_http(monkeypatch: pytest.MonkeyPatch, fake_request: Callable[..., Any]) -> None:
    def _fake_http_request(self: Any, method: str, path: str, **kwargs: Any) -> Any:
        return fake_request(method, f"{self.api_url}{path}", **kwargs)

    monkeypatch.setattr("rebase.client.Client._http_request", _fake_http_request)
