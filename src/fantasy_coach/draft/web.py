"""The companion's local web layer — stdlib only, one page, one JSON endpoint.

``http.server.ThreadingHTTPServer`` is deliberately chosen over Flask: the
whole surface is two GET routes serving one browser on localhost, so a
framework would add a dependency (and a draft-day install risk) for nothing.
The threading server matters, though — the page polls ``/api/state`` every 2s
while also fetching ``/``, and a single-threaded server would serialize those
behind a slow request.

Routes:

* ``/`` — the dark draft-board page (``page.html``, shipped next to this
  module). It polls the API itself; no server push needed at a 2s cadence.
* ``/api/state`` — the loop's latest snapshot as JSON. The snapshot carries
  its own ``age_seconds``/``stale`` so the page can grey out a recommendation
  the moment polling falls behind the room.

The server binds ``127.0.0.1`` — this is a private cockpit, not a site.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

__all__ = ["CompanionServer", "load_page"]

logger = logging.getLogger(__name__)

_PAGE_PATH = Path(__file__).with_name("page.html")


def load_page() -> str:
    """The companion page HTML (read fresh so dev edits show on reload)."""
    return _PAGE_PATH.read_text(encoding="utf-8")


class CompanionServer:
    """Serves the draft page + state JSON for one :class:`DraftLoop`.

    Args:
        snapshot_func: Zero-arg callable returning the JSON-ready snapshot
            (normally ``loop.snapshot`` — already thread-safe).
        host: Bind address; loopback by default on purpose.
        port: TCP port; ``0`` lets the OS pick (tests). :attr:`port` reports
            the actual bound port either way.
    """

    def __init__(
        self,
        snapshot_func: Callable[[], dict],
        *,
        host: str = "127.0.0.1",
        port: int = 8787,
    ) -> None:
        self._snapshot_func = snapshot_func
        handler = self._make_handler()
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self.httpd.daemon_threads = True
        self._thread: threading.Thread | None = None

    def _make_handler(self) -> type[BaseHTTPRequestHandler]:
        snapshot_func = self._snapshot_func

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                path = self.path.split("?", 1)[0]
                if path in ("/", "/index.html"):
                    body = load_page().encode("utf-8")
                    self._send(200, "text/html; charset=utf-8", body)
                elif path == "/api/state":
                    body = json.dumps(snapshot_func()).encode("utf-8")
                    self._send(200, "application/json", body)
                else:
                    self._send(404, "text/plain", b"not found")

            def _send(self, status: int, ctype: str, body: bytes) -> None:
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, fmt: str, *args: object) -> None:
                # Route request logs to logging.debug — a 2s poll would
                # otherwise bury the draft loop's own output in stderr noise.
                logger.debug("http: " + fmt, *args)

        return Handler

    @property
    def host(self) -> str:
        """The bound address."""
        return self.httpd.server_address[0]

    @property
    def port(self) -> int:
        """The actual bound port (resolves ``port=0``)."""
        return self.httpd.server_address[1]

    @property
    def url(self) -> str:
        """The page URL to open next to the Yahoo draft room."""
        return f"http://{self.host}:{self.port}/"

    def start(self) -> None:
        """Serve in a daemon thread (returns immediately)."""
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, name="companion-http", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Shut the server down and release the socket."""
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> "CompanionServer":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
