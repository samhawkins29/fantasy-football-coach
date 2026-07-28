"""Fantasy Coach — a season-long AI Fantasy Football assistant coach.

This package currently implements **M1: Auth / OAuth 2.0 + token store**, the
shared foundation every other module builds on (see FANTASY_COACH_FRAMEWORK.md
§5). The public auth surface that M2 (the Yahoo client) will consume is
re-exported here for convenience.
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
from fantasy_coach.config import Config

__all__ = [
    "__version__",
    "Config",
    "Token",
    "TokenStore",
    "YahooOAuthClient",
    "AuthedClient",
    "get_authed_client",
]
