"""Tests for the Yahoo OAuth client — consent URL, code exchange, refresh.

All HTTP is served by httpx.MockTransport; nothing hits the network.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from fantasy_coach.auth.oauth import OAuthError, YahooOAuthClient
from tests.conftest import make_mock_client


def make_client(handler) -> YahooOAuthClient:
    return YahooOAuthClient(
        client_id="my-id",
        client_secret="my-secret",
        redirect_uri="https://localhost:8000/callback",
        scope="fspt-r",
        http=make_mock_client(handler),
    )


# -- consent URL (pure, no network) ----------------------------------------


def test_create_authorization_url_contains_expected_params():
    client = YahooOAuthClient("my-id", "sec", "https://localhost:8000/callback")
    url, state = client.create_authorization_url()
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    assert parsed.netloc == "api.login.yahoo.com"
    assert params["client_id"] == ["my-id"]
    assert params["redirect_uri"] == ["https://localhost:8000/callback"]
    assert params["response_type"] == ["code"]
    assert params["scope"] == ["fspt-r"]
    assert params["state"] == [state]
    assert state  # non-empty CSRF token generated


def test_create_authorization_url_honors_supplied_state():
    client = YahooOAuthClient("my-id", "sec", "https://localhost:8000/callback")
    url, state = client.create_authorization_url(state="fixed-state")
    assert state == "fixed-state"
    assert "state=fixed-state" in url


# -- code exchange ----------------------------------------------------------


def test_fetch_token_success():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = parse_qs(request.content.decode())
        return httpx.Response(
            200,
            json={
                "access_token": "acc-1",
                "refresh_token": "ref-1",
                "expires_in": 3600,
                "token_type": "bearer",
                "xoauth_yahoo_guid": "GUID",
            },
        )

    client = make_client(handler)
    token = client.fetch_token("the-code")

    assert token.access_token == "acc-1"
    assert token.refresh_token == "ref-1"
    assert token.xoauth_yahoo_guid == "GUID"
    assert token.seconds_until_expiry() > 3500
    # request shape
    assert captured["url"] == "https://api.login.yahoo.com/oauth2/get_token"
    assert captured["auth"].startswith("Basic ")
    assert captured["body"]["grant_type"] == ["authorization_code"]
    assert captured["body"]["code"] == ["the-code"]


def test_fetch_token_http_error_raises_oautherror():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant",
                                         "error_description": "bad code"})

    client = make_client(handler)
    with pytest.raises(OAuthError) as exc:
        client.fetch_token("bad")
    assert "bad code" in str(exc.value)


# -- refresh ----------------------------------------------------------------


def test_refresh_token_success_and_request_shape():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = parse_qs(request.content.decode())
        return httpx.Response(
            200,
            json={
                "access_token": "acc-2",
                "refresh_token": "ref-2",
                "expires_in": 3600,
            },
        )

    client = make_client(handler)
    token = client.refresh_token("old-refresh")
    assert token.access_token == "acc-2"
    assert token.refresh_token == "ref-2"
    assert captured["body"]["grant_type"] == ["refresh_token"]
    assert captured["body"]["refresh_token"] == ["old-refresh"]


def test_refresh_keeps_old_refresh_token_when_omitted():
    def handler(request: httpx.Request) -> httpx.Response:
        # Yahoo sometimes omits refresh_token on refresh.
        return httpx.Response(200, json={"access_token": "acc-3", "expires_in": 3600})

    client = make_client(handler)
    token = client.refresh_token("keep-me")
    assert token.access_token == "acc-3"
    assert token.refresh_token == "keep-me"


def test_error_payload_with_200_status_still_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "token_expired"})

    client = make_client(handler)
    with pytest.raises(OAuthError):
        client.refresh_token("x")


def test_non_json_body_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    client = make_client(handler)
    with pytest.raises(OAuthError):
        client.fetch_token("x")


def test_context_manager_closes_owned_client():
    with YahooOAuthClient("id", "sec", "https://localhost/cb") as client:
        assert client.http is not None
    # after exit the owned client is closed/cleared
    assert client._http is None
