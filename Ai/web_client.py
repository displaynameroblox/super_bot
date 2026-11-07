"""Run the local web client: a browser UI on this computer that talks to a Super Bot server.

    python -m Ai.web_client --url https://HOST:8000

The server can be on another machine. Requests travel over the same TLS +
end-to-end sealed, fingerprint-pinned transport as the terminal clients. The page is
served on 127.0.0.1 only, so nothing else on the network can reach it.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import webbrowser
from pathlib import Path
from typing import Any

import uvicorn

try:
    from . import account_app, client_data, chat_app, preflight, web_app
    from . import transport
except ImportError:
    from Ai import client_data
    import account_app
    import chat_app
    import preflight
    import transport
    import web_app

DEFAULT_WEB_PORT = 8001
REQUEST_TIMEOUT = 500


def make_backend(server_url: str, requester=None):
    """Forward browser calls to the server through the encrypted transport."""
    send = requester or client_data.request

    async def backend(method: str, path: str, body: Any, bearer: str | None) -> tuple[int, Any]:
        try:
            return await asyncio.to_thread(
                send, server_url, method, path, body, bearer or "", timeout=REQUEST_TIMEOUT
            )
        except transport.TransportError as exc:
            return 502, {"detail": str(exc)}

    return backend


def main() -> int:
    parser = argparse.ArgumentParser(description="Local browser client for a Super Bot server")
    parser.add_argument("--url", default=chat_app.DEFAULT_URL, help="server URL; discovered if omitted")
    parser.add_argument("--port", type=int, default=DEFAULT_WEB_PORT, help="local web port (default 8001)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    parser.add_argument("--data-file", type=Path, default=chat_app.default_data_file(),
                        help="file next to which the trusted-servers list is kept")
    parser.add_argument("--skip-tests", action="store_true", help="disable the optional SUPER_BOT_RUN_TESTS startup checks")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        print("--port must be between 1 and 65535", file=sys.stderr)
        return 2
    if not preflight.ensure_tests_pass(args.skip_tests):
        return 1
    server_url = account_app.resolve_server(args.url, args.data_file)
    if server_url is None:
        return 1
    app = web_app.build_web_app(make_backend(server_url), args.port)
    address = f"http://127.0.0.1:{args.port}"
    print(f"Web client for {server_url}\nOpen {address} (this computer only). Press Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        webbrowser.open(address)
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, server_header=False, log_level="warning")
    except OSError as exc:
        print(f"Could not listen on port {args.port}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
