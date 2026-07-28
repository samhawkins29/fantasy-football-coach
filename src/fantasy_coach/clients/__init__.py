"""Clients subpackage — M2: the Yahoo Fantasy read client.

:class:`YahooClient` is a typed, offline-testable read wrapper over the Yahoo
Fantasy API, built on M1's :func:`fantasy_coach.auth.get_authed_client` (which
already handles bearer injection + transparent token refresh). It parses Yahoo's
notoriously nested JSON (see :mod:`fantasy_coach.clients.parsers`) into the clean
dataclasses in :mod:`fantasy_coach.clients.models`, paginates the player pool,
and routes every call through a conservative
:class:`~fantasy_coach.clients.throttle.Throttle` (framework §7).

Typical use (once the user has authenticated via M1)::

    from fantasy_coach import Config, get_authed_client
    from fantasy_coach.clients import YahooClient

    authed = get_authed_client(Config.load())
    yahoo = YahooClient(authed)
    leagues = yahoo.get_user_leagues(season="2026")
    settings = yahoo.get_league_settings(leagues[0].league_key)

No live Yahoo data flows until that authenticated session exists (M1).
"""

from __future__ import annotations

from fantasy_coach.clients.keys import (
    build_league_key,
    build_player_key,
    build_team_key,
    game_key_of,
    league_key_of,
    split_league_key,
)
from fantasy_coach.clients.models import (
    DraftAnalysis,
    DraftPick,
    Game,
    League,
    LeagueSettings,
    Manager,
    Matchup,
    MatchupTeam,
    Player,
    PlayerIdentity,
    PlayerRank,
    RosterPosition,
    RosterSlot,
    StatCategory,
    Team,
    TeamRoster,
    Transaction,
    TransactionPlayer,
)
from fantasy_coach.clients.parsers import YahooParseError
from fantasy_coach.clients.throttle import (
    DEFAULT_MIN_INTERVAL,
    DRAFT_POLL_INTERVAL,
    NullThrottle,
    Throttle,
)
from fantasy_coach.clients.yahoo import (
    PLAYER_PAGE_SIZE,
    YahooAPIError,
    YahooClient,
)

__all__ = [
    # client
    "YahooClient",
    "YahooAPIError",
    "YahooParseError",
    "PLAYER_PAGE_SIZE",
    # throttle
    "Throttle",
    "NullThrottle",
    "DEFAULT_MIN_INTERVAL",
    "DRAFT_POLL_INTERVAL",
    # models
    "Game",
    "League",
    "LeagueSettings",
    "RosterPosition",
    "StatCategory",
    "Manager",
    "Team",
    "Player",
    "PlayerRank",
    "PlayerIdentity",
    "DraftAnalysis",
    "RosterSlot",
    "TeamRoster",
    "DraftPick",
    "Transaction",
    "TransactionPlayer",
    "Matchup",
    "MatchupTeam",
    # keys
    "build_league_key",
    "build_team_key",
    "build_player_key",
    "game_key_of",
    "league_key_of",
    "split_league_key",
]
