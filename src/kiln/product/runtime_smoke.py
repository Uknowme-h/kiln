"""Host-only HTTP smoke: start the app briefly and GET /."""

from __future__ import annotations

import urllib.error
import urllib.request
from pathlib import Path

from kiln.product.dev_server import resolve_start_command, start_dev_server

# Re-export for callers that imported from this module historically.
__all__ = ["resolve_start_command", "run_runtime_smoke"]


def run_runtime_smoke(
    root: Path,
    *,
    paths: list[str] | None = None,
    timeout: float = 20.0,
    base_url: str | None = None,
) -> list[str]:
    """GET listed paths against a running (or briefly started) app; return gaps.

    An empty list means pass. If no start command is found and no ``base_url``
    is given, returns [] (advisory skip).
    """
    root = Path(root)
    gaps: list[str] = []
    server = None
    url_base = (base_url or "").rstrip("/")
    try:
        if not url_base:
            server, start_gap = start_dev_server(root, timeout=timeout)
            if start_gap:
                return [f"runtime_smoke: {start_gap}"]
            if server is None:
                return []
            url_base = server.base_url
        check_paths = paths or ["/"]
        for path in check_paths:
            rel = path if path.startswith("/") else f"/{path}"
            url = f"{url_base}{rel}"
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    status = getattr(resp, "status", None) or resp.getcode()
                    body = resp.read()
            except urllib.error.HTTPError as exc:
                gaps.append(f"runtime_smoke: GET {path} returned HTTP {exc.code}")
                continue
            except Exception as exc:  # noqa: BLE001 — surface any client failure
                gaps.append(f"runtime_smoke: GET {path} failed: {exc}")
                continue
            if int(status) >= 400:
                gaps.append(f"runtime_smoke: GET {path} returned HTTP {status}")
            elif not body:
                gaps.append(f"runtime_smoke: GET {path} returned an empty body")
    finally:
        if server is not None:
            server.stop()
    return gaps
