"""Tests for AuthedClient — bearer injection, pre-emptive refresh, 401 retry.

The OAuth token endpoint and the Yahoo API are both faked with
httpx.MockTransport. No network access occurs.
"""

from __future__ import annotations

import time

import httpx
import pytest

from fantasy_coach.auth.oauth import YahooOAuthClient
from fantasy_coach.auth.session import (
    AuthedClient,
    NotAuthenticatedError,
    get_authed_client,
)
from fantasy_coach.auth.token_store import Token, TokenStore
from fantasy_coach.config import Config
from tests.conftest import make_mock_client


def build_oauth(refresh_handler) -> YahooOAuthClient:
    """OAuth client whose token endpoint is served by ``refresh_handler``."""
    return YahooOAuthClient(
        client_id="id",
        client_secret="sec",
        redirect_uri="https://localhost:8000/callback",
        http=make_mock_client(refresh_handler),
    )


def refresh_returns(access="refreshed-access", refresh="refreshed-refresh"):
    """A token-endpoint handler that always returns the given tokens."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"access_token": access, "refresh_token": refresh, "expires_in": 3600},
        )

    return handler


# -- token validation / refresh --------------------------------------------


def test_valid_token_returns_fresh_without_refresh(token_store, fresh_token):
    token_store.save(fresh_token)

    def fail(request):  # refresh must NOT be called
        raise AssertionError("refresh should not happen for a fresh token")

    client = AuthedClient(build_oauth(fail), token_store)
    token = client.valid_token()
    assert token.access_token == "access-abc"


def test_valid_token_refreshes_when_near_expiry(token_store):
    # expires in 60s, leeway 300s -> considered near-expiry
    near = Token("old-access", "old-refresh", expires_at=time.time() + 60)
    token_store.save(near)

    client = AuthedClient(build_oauth(refresh_returns()), token_store, refresh_leeway=300)
    token = client.valid_token()

    assert token.access_token == "refreshed-access"
    # persisted to the store
    assert token_store.load().access_token == "refreshed-access"


def test_valid_token_refreshes_when_expired(token_store):
    expired = Token("old", "old-refresh", expires_at=time.time() - 10)
    token_store.save(expired)
    client = AuthedClient(build_oauth(refresh_returns()), token_store)
    assert client.valid_token().access_token == "refreshed-access"


def test_valid_token_raises_when_no_token(token_store):
    client = AuthedClient(build_oauth(refresh_returns()), token_store)
    with pytest.raises(NotAuthenticatedError):
        client.valid_token()


# -- request path: bearer injection + 401 retry -----------------------------


def test_request_injects_bearer_and_succeeds(token_store, fresh_token):
    token_store.save(fresh_token)
    seen = {}

    def api_handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"ok": True})

    client = AuthedClient(
        build_oauth(refresh_returns()),
        token_store,
        http=make_mock_client(api_handler),
    )
    resp = client.get("https://fantasysports.yahooapis.com/fantasy/v2/game/nfl")
    assert resp.status_code == 200
    assert seen["auth"] == "Bearer access-abc"


def test_request_refreshes_and_retries_on_401(token_store, fresh_token):
    token_store.save(fresh_token)
    calls = {"n": 0, "auths": []}

    def api_handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        calls["auths"].append(request.headers.get("authorization"))
        if calls["n"] == 1:
            return httpx.Response(401, json={"error": "token_expired"})
        return httpx.Response(200, json={"ok": True})

    client = AuthedClient(
        build_oauth(refresh_returns(access="new-access")),
        token_store,
        http=make_mock_client(api_handler),
    )
    resp = client.get("https://example/api")

    assert resp.status_code == 200
    assert calls["n"] == 2
    assert calls["auths"][0] == "Bearer access-abc"       # first, old token
    assert calls["auths"][1] == "Bearer new-access"       # retry, refreshed
    # store updated by the forced refresh
    assert token_store.load().access_token == "new-access"


def test_request_401_retry_happens_only_once(token_store, fresh_token):
    token_store.save(fresh_token)
    calls = {"n": 0}

    def api_handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": "nope"})  # always 401

    client = AuthedClient(
        build_oauth(refresh_returns()),
        token_store,
        http=make_mock_client(api_handler),
    )
    resp = client.get("https://example/api")
    assert resp.status_code == 401
    assert calls["n"] == 2  # original + exactly one retry


def test_caller_headers_are_preserved(token_store, fresh_token):
    token_store.save(fresh_token)
    seen = {}

    def api_handler(request: httpx.Request) -> httpx.Response:
        seen["accept"] = request.headers.get("accept")
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={})

    client = AuthedClient(
        build_oauth(refresh_returns()),
        token_store,
        http=make_mock_client(api_handler),
    )
    client.get("https://example/api", headers={"Accept": "application/json"})
    assert seen["accept"] == "application/json"
    assert seen["auth"] == "Bearer access-abc"


# -- token_status ------------------------------------------------------------


def test_token_status_unauthenticated(token_store):
    client = AuthedClient(build_oauth(refresh_returns()), token_store)
    assert client.token_status() == {"authenticated": False}


def test_token_status_reports_state(token_store, fresh_token):
    token_store.save(fresh_token)
    client = AuthedClient(build_oauth(refresh_returns()), token_store, refresh_leeway=300)
    info = client.token_status()
    assert info["authenticated"] is True
    assert info["expired"] is False
    assert info["near_expiry"] is False
    assert info["yahoo_guid"] == "GUID123"
    assert info["scope"] == "fspt-r"


def test_token_status_flags_near_expiry(token_store):
    token_store.save(Token("a", "r", expires_at=time.time() + 100))
    client = AuthedClient(build_oauth(refresh_returns()), token_store, refresh_leeway=300)
    info = client.token_status()
    assert info["near_expiry"] is True
    assert info["expired"] is False


# -- get_authed_client factory ----------------------------------------------


def test_get_authed_client_from_config(token_path, fresh_token):
    TokenStore(token_path).save(fresh_token)
    cfg = Config(
        yahoo_client_id="id",
        yahoo_client_secret="sec",
        yahoo_redirect_uri="https://localhost:8000/callback",
        token_path=token_path,
    )

    def api_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client = get_authed_client(cfg, http=make_mock_client(api_handler))
    assert client.valid_token().access_token == "access-abc"


def test_get_authed_client_requires_oauth_config(token_path):
    cfg = Config(token_path=token_path)  # no client id/secret
    from fantasy_coach.config import ConfigError

    with pytest.raises(ConfigError):
        get_authed_client(cfg)
