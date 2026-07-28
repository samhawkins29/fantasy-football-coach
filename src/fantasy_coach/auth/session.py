"""Authenticated HTTP client — the interface M2 (the Yahoo client) builds on.

:class:`AuthedClient` wraps an :class:`~fantasy_coach.auth.oauth.YahooOAuthClient`
and a :class:`~fantasy_coach.auth.token_store.TokenStore` and gives callers a
requests-like surface (``get``/``post``/``request``) that:

* injects the ``Authorization: Bearer <token>`` header on every request,
* transparently refreshes the access token when it is expired or *near* expiry
  (pre-emptive refresh, so a live draft never drops mid-round — framework §7),
* on a ``401 Unauthorized`` refreshes once and retries the request,
* persists every new token back to the store.

M2 should depend only on this class (via :func:`get_authed_client`) and never
touch tokens or refresh logic directly.
"""

from __future__ import annotations

import threading

import httpx

from fantasy_coach.auth.oauth import OAuthError, YahooOAuthClient
from fantasy_coach.auth.token_store import Token, TokenStore
from fantasy_coach.config import Config

# Refresh this many seconds *before* the real expiry. Yahoo access tokens live
# ~3600s; refreshing with 5 min of headroom means we never present an
# about-to-die token to the API (framework §7 "token pre-refresh").
DEFAULT_REFRESH_LEEWAY = 300.0

YAHOO_API_BASE = "https://fantasysports.yahooapis.com/fantasy/v2"


class NotAuthenticatedError(RuntimeError):
    """Raised when no token is stored yet — the user must run ``login`` first."""


class AuthedClient:
    """A token-injecting, auto-refreshing HTTP client.

    Args:
        oauth: Configured Yahoo OAuth client (does the token dance).
        store: Token store to read/write persisted tokens.
        refresh_leeway: Refresh the access token when it is within this many
            seconds of expiry.
        http: Optional ``httpx.Client`` for the *API* calls (separate from the
            token-endpoint client inside ``oauth``). Injectable for tests.
        base_url: Base URL prepended to relative request paths.
        timeout: Per-request timeout (seconds) for API calls.
    """

    def __init__(
        self,
        oauth: YahooOAuthClient,
        store: TokenStore,
        *,
        refresh_leeway: float = DEFAULT_REFRESH_LEEWAY,
        http: httpx.Client | None = None,
        base_url: str = YAHOO_API_BASE,
        timeout: float = 30.0,
    ) -> None:
        self._oauth = oauth
        self._store = store
        self._refresh_leeway = refresh_leeway
        self._base_url = base_url
        self._timeout = timeout
        self._http = http
        self._owns_http = http is None
        self._lock = threading.Lock()  # guards refresh + store writes
        self._token: Token | None = None

    # -- lifecycle -----------------------------------------------------------

    @property
    def http(self) -> httpx.Client:
        """The ``httpx.Client`` used for API calls (created lazily)."""
        if self._http is None:
            self._http = httpx.Client(base_url=self._base_url, timeout=self._timeout)
            self._owns_http = True
        return self._http

    def close(self) -> None:
        """Close owned HTTP clients (this client's and the OAuth client's)."""
        if self._http is not None and self._owns_http:
            self._http.close()
            self._http = None
        self._oauth.close()

    def __enter__(self) -> "AuthedClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- token management ----------------------------------------------------

    def _load_token(self) -> Token:
        """Return the in-memory token, loading from the store on first access."""
        if self._token is None:
            self._token = self._store.load()
        if self._token is None:
            raise NotAuthenticatedError(
                "No stored token. Run `python -m fantasy_coach login` first."
            )
        return self._token

    def valid_token(self, *, force_refresh: bool = False) -> Token:
        """Return a non-expired token, refreshing + persisting if necessary.

        Args:
            force_refresh: Refresh even if the current token still looks valid
                (used by the 401 retry path).

        Returns:
            A :class:`Token` guaranteed fresh within ``refresh_leeway``.
        """
        with self._lock:
            token = self._load_token()
            if force_refresh or token.is_expired(leeway=self._refresh_leeway):
                token = self._refresh_locked(token)
            return token

    def _refresh_locked(self, token: Token) -> Token:
        """Refresh ``token`` and persist it. Caller must hold ``self._lock``."""
        new_token = self._oauth.refresh_token(token.refresh_token)
        self._store.save(new_token)
        self._token = new_token
        return new_token

    def refresh(self) -> Token:
        """Force a refresh now and persist the result."""
        return self.valid_token(force_refresh=True)

    def token_status(self) -> dict:
        """Return a JSON-friendly snapshot of the stored token's state.

        Does not refresh. Returns ``{"authenticated": False}`` if nothing is
        stored. No network I/O.
        """
        token = self._store.load()
        if token is None:
            return {"authenticated": False}
        seconds = token.seconds_until_expiry()
        return {
            "authenticated": True,
            "expires_at": token.expires_at,
            "seconds_until_expiry": seconds,
            "expired": token.is_expired(),
            "near_expiry": token.is_expired(leeway=self._refresh_leeway),
            "token_type": token.token_type,
            "scope": token.scope,
            "yahoo_guid": token.xoauth_yahoo_guid,
            "refresh_leeway": self._refresh_leeway,
        }

    # -- request surface -----------------------------------------------------

    def request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        """Send an authenticated request, refreshing on expiry and on 401.

        The bearer header is injected from a freshly validated token. If the
        server still returns ``401`` (e.g. the token was revoked server-side),
        we force one refresh and retry exactly once.

        Args:
            method: HTTP method (``"GET"``, ``"POST"``, ...).
            url: Absolute URL or a path relative to ``base_url``.
            **kwargs: Passed through to ``httpx.Client.request``. A ``headers``
                mapping is merged with (and cannot override) the auth header.

        Returns:
            The ``httpx.Response``.
        """
        token = self.valid_token()
        resp = self._send(method, url, token, kwargs)
        if resp.status_code == 401:
            token = self.valid_token(force_refresh=True)
            resp = self._send(method, url, token, kwargs)
        return resp

    def _send(
        self, method: str, url: str, token: Token, kwargs: dict
    ) -> httpx.Response:
        """Inject the bearer header and dispatch a single request."""
        headers = dict(kwargs.pop("headers", None) or {})
        headers["Authorization"] = token.authorization_header
        return self.http.request(method, url, headers=headers, **kwargs)

    def get(self, url: str, **kwargs: object) -> httpx.Response:
        """Authenticated GET. See :meth:`request`."""
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: object) -> httpx.Response:
        """Authenticated POST. See :meth:`request`."""
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: object) -> httpx.Response:
        """Authenticated PUT. See :meth:`request`."""
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: object) -> httpx.Response:
        """Authenticated DELETE. See :meth:`request`."""
        return self.request("DELETE", url, **kwargs)


def get_authed_client(
    config: Config,
    *,
    http: httpx.Client | None = None,
    oauth_http: httpx.Client | None = None,
    refresh_leeway: float = DEFAULT_REFRESH_LEEWAY,
) -> AuthedClient:
    """Build an :class:`AuthedClient` from a :class:`Config`.

    This is the one-call entry point M2 uses to obtain an authenticated client.
    It does not perform any network I/O and does not require a stored token to
    exist — the first request (or ``valid_token``) is what triggers a load.

    Args:
        config: Application configuration (OAuth creds + token path).
        http: Optional injected client for API calls (tests).
        oauth_http: Optional injected client for token-endpoint calls (tests).
        refresh_leeway: Pre-emptive refresh window in seconds.

    Returns:
        A ready-to-use :class:`AuthedClient`.

    Raises:
        ConfigError: If required OAuth configuration is missing.
    """
    config.require_oauth()
    oauth = YahooOAuthClient(
        client_id=config.yahoo_client_id,
        client_secret=config.yahoo_client_secret,
        redirect_uri=config.yahoo_redirect_uri,
        scope=config.yahoo_scope,
        http=oauth_http,
    )
    store = TokenStore(config.token_path)
    return AuthedClient(oauth, store, refresh_leeway=refresh_leeway, http=http)


__all__ = [
    "AuthedClient",
    "NotAuthenticatedError",
    "get_authed_client",
    "DEFAULT_REFRESH_LEEWAY",
    "YAHOO_API_BASE",
]
