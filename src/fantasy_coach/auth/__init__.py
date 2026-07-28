"""Auth subpackage — Yahoo OAuth 2.0 flow, token store, and authed session.

Public surface (import from ``fantasy_coach.auth``):

* :class:`Token`, :class:`TokenStore` — the token model + local persistence.
* :class:`YahooOAuthClient` — consent URL, code exchange, refresh.
* :class:`AuthedClient` / :func:`get_authed_client` — the injecting,
  auto-refreshing HTTP client M2 consumes.
"""

from __future__ import annotations

from fantasy_coach.auth.oauth import OAuthError, YahooOAuthClient
from fantasy_coach.auth.session import (
    AuthedClient,
    NotAuthenticatedError,
    get_authed_client,
)
from fantasy_coach.auth.token_store import Token, TokenStore

__all__ = [
    "Token",
    "TokenStore",
    "YahooOAuthClient",
    "OAuthError",
    "AuthedClient",
    "NotAuthenticatedError",
    "get_authed_client",
]
