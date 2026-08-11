"""Yahoo OAuth 2.0 authorization-code client.

This module *owns* the token dance (framework §2.4: "Own the OAuth token layer
yourself rather than fully trusting yahoo_oauth's staleness"). It does three
things and nothing else:

1. Build the consent URL (+ CSRF ``state``) — via ``requests-oauthlib``'s pure,
   network-free ``OAuth2Session.authorization_url``.
2. Exchange an authorization ``code`` for a :class:`Token`.
3. Refresh an access token using the refresh token.

All HTTP goes through an injectable ``httpx.Client``, so the whole module is
unit-testable offline by passing a client backed by ``httpx.MockTransport``.
"""

from __future__ import annotations

import base64

import httpx
from requests_oauthlib import OAuth2Session

from fantasy_coach.config import YAHOO_AUTHORIZE_URL, YAHOO_TOKEN_URL
from fantasy_coach.auth.token_store import Token


class OAuthError(RuntimeError):
    """Raised when the token endpoint returns an error or malformed response."""


class YahooOAuthClient:
    """Performs the Yahoo OAuth 2.0 authorization-code grant.

    Args:
        client_id: Yahoo app Client ID (Consumer Key).
        client_secret: Yahoo app Client Secret.
        redirect_uri: Registered HTTPS redirect URI (must match the app).
        scope: OAuth scope. Default is empty = send NO ``scope`` parameter,
            which is what the Fantasy API needs: Yahoo rejects ``fspt-r`` and
            an empty ``scope=`` with ``invalid_scope``, and an ``openid``-only
            token gets 401 ``additional_authorization_required`` from Fantasy.
            Omitting the parameter yields a broad token that can access
            Fantasy (same as yahoo_oauth/yahoofantasy). Set a value only if
            you deliberately want a narrower token (e.g. ``openid``).
        http: Optional ``httpx.Client`` to use for token requests. If omitted,
            one is created lazily and owned by this instance. Inject a client
            backed by ``httpx.MockTransport`` in tests to avoid the network.
        timeout: Per-request timeout (seconds) for token calls.
    """

    authorize_url = YAHOO_AUTHORIZE_URL
    token_url = YAHOO_TOKEN_URL

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        *,
        scope: str | None = "",
        http: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        # Blank/whitespace scope means "no scope" — the param must be omitted
        # from the authorize URL entirely (Yahoo returns invalid_scope for an
        # empty ``scope=``).
        self.scope = (scope or "").strip()
        self._timeout = timeout
        self._http = http
        self._owns_http = http is None

    # -- context management / lifecycle -------------------------------------

    @property
    def http(self) -> httpx.Client:
        """The underlying ``httpx.Client``, created on first use if needed."""
        if self._http is None:
            self._http = httpx.Client(timeout=self._timeout)
            self._owns_http = True
        return self._http

    def close(self) -> None:
        """Close the HTTP client if this instance created it."""
        if self._http is not None and self._owns_http:
            self._http.close()
            self._http = None

    def __enter__(self) -> "YahooOAuthClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- step 1: consent URL -------------------------------------------------

    def create_authorization_url(self, *, state: str | None = None) -> tuple[str, str]:
        """Build the Yahoo consent URL the user visits to authorize the app.

        Uses ``requests-oauthlib`` purely as a URL builder — no network call is
        made here. If ``state`` is not supplied, a random CSRF ``state`` is
        generated; the caller should verify it against the value Yahoo echoes
        back on the redirect.

        Args:
            state: Optional CSRF token to embed. Auto-generated if omitted.

        Returns:
            ``(authorization_url, state)``.
        """
        session = OAuth2Session(
            client_id=self.client_id,
            redirect_uri=self.redirect_uri,
            # None (not "") when no scope is configured, so oauthlib omits the
            # ``scope`` parameter from the URL altogether.
            scope=self.scope or None,
            state=state,
        )
        url, used_state = session.authorization_url(self.authorize_url)
        return url, used_state

    # -- step 2 & 3: token endpoint -----------------------------------------

    def fetch_token(self, code: str) -> Token:
        """Exchange an authorization ``code`` for a fresh :class:`Token`.

        Args:
            code: The ``code`` query param captured from the redirect.

        Returns:
            A :class:`Token` with an absolute expiry.

        Raises:
            OAuthError: On a non-2xx response or an error payload.
        """
        data = self._token_request(
            {
                "grant_type": "authorization_code",
                "redirect_uri": self.redirect_uri,
                "code": code,
            }
        )
        return Token.from_response(data)

    def refresh_token(self, refresh_token: str) -> Token:
        """Mint a new access token from a ``refresh_token``.

        Yahoo sometimes omits ``refresh_token`` from the refresh response; we
        carry the existing one forward so the store never loses it.

        Args:
            refresh_token: The long-lived refresh token.

        Returns:
            A new :class:`Token`.

        Raises:
            OAuthError: On a non-2xx response or an error payload.
        """
        data = self._token_request(
            {
                "grant_type": "refresh_token",
                "redirect_uri": self.redirect_uri,
                "refresh_token": refresh_token,
            }
        )
        return Token.from_response(data, fallback_refresh_token=refresh_token)

    # -- internals -----------------------------------------------------------

    def _basic_auth_header(self) -> str:
        """HTTP Basic credentials (``client_id:client_secret``) for the token endpoint."""
        raw = f"{self.client_id}:{self.client_secret}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def _token_request(self, form: dict[str, str]) -> dict:
        """POST to Yahoo's token endpoint and return the parsed JSON body.

        Yahoo authenticates the client via HTTP Basic (id:secret) and expects a
        urlencoded form body. Raises :class:`OAuthError` on transport failure,
        a non-2xx status, or an ``{"error": ...}`` payload.
        """
        headers = {
            "Authorization": self._basic_auth_header(),
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        }
        try:
            resp = self.http.post(self.token_url, data=form, headers=headers)
        except httpx.HTTPError as exc:  # network/transport-level failure
            raise OAuthError(f"token request to {self.token_url} failed: {exc}") from exc

        # Parse JSON if we can; Yahoo returns JSON for both success and error.
        try:
            body = resp.json()
        except ValueError:
            body = None

        if resp.status_code >= 400 or (isinstance(body, dict) and body.get("error")):
            detail = ""
            if isinstance(body, dict):
                detail = body.get("error_description") or body.get("error") or ""
            raise OAuthError(
                f"token endpoint returned HTTP {resp.status_code}"
                + (f": {detail}" if detail else f": {resp.text[:200]}")
            )

        if not isinstance(body, dict):
            raise OAuthError("token endpoint returned a non-JSON body")
        return body
