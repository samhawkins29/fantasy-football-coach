"""The companion web layer — real sockets on an ephemeral port, no browser."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from fantasy_coach.draft.web import CompanionServer, load_page


@pytest.fixture
def server():
    snap = {"mode": "simulation", "available": [], "stale": False}
    with CompanionServer(lambda: snap, port=0) as srv:  # port 0 -> OS-assigned
        yield srv


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.headers.get("Content-Type", ""), resp.read()


def test_page_is_served_at_root(server):
    status, ctype, body = _get(server.url)
    assert status == 200
    assert "text/html" in ctype
    assert b"FANTASY" in body and b"/api/state" in body


def test_state_endpoint_serves_the_snapshot_json(server):
    status, ctype, body = _get(server.url + "api/state")
    assert status == 200
    assert "application/json" in ctype
    assert json.loads(body) == {"mode": "simulation", "available": [], "stale": False}


def test_unknown_path_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server.url + "nope")
    assert exc.value.code == 404


def test_page_html_has_the_live_ui_hooks():
    page = load_page()
    for marker in ("hero", "avail", "tiers", "roster", "recent", "setInterval"):
        assert marker in page
