"""Fantasy Coach — a season-long AI Fantasy Football assistant coach.

This package implements **M1: Auth / OAuth 2.0 + token store** and
**M2: the Yahoo Fantasy read client** (:class:`fantasy_coach.clients.YahooClient`),
the shared foundation the projection engine and draft monitor build on (see
FANTASY_COACH_FRAMEWORK.md §5). The public auth surface M2 consumes, and the
Yahoo client itself, are re-exported here for convenience.
"""

from __future__ import annotations

__version__ = "0.1.0"

from fantasy_coach.auth import (
    AuthedClient,
    Token,
    TokenStore,
    YahooOAuthClient,
    get_authed_client,
)
from fantasy_coach.clients import YahooClient
from fantasy_coach.config import Config

__all__ = [
    "__version__",
    "Config",
    "Token",
    "TokenStore",
    "YahooOAuthClient",
    "AuthedClient",
    "get_authed_client",
    "YahooClient",
]
