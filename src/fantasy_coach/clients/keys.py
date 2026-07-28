"""Yahoo resource-key construction and parsing (framework §2.3).

Yahoo addresses everything with dotted composite keys::

    game_key   449                     (per sport, per season)
    league_key 449.l.123456            {game_key}.l.{league_id}
    team_key   449.l.123456.t.4        {league_key}.t.{team_id}
    player_key 449.p.31883             {game_key}.p.{player_id}

Note that a ``player_key`` hangs off the *game*, not the league — the same
player has the same key in every 2026 league, which is exactly what M3's
id-crosswalk needs. These helpers are pure string manipulation, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "NFL_GAME_CODE",
    "LeagueKey",
    "build_league_key",
    "build_team_key",
    "build_player_key",
    "game_key_of",
    "league_key_of",
    "split_league_key",
]

#: Yahoo's sport code for the NFL. The alias ``"nfl"`` also works anywhere a
#: ``game_key`` is expected and resolves to the current in-season game.
NFL_GAME_CODE = "nfl"


@dataclass(frozen=True, slots=True)
class LeagueKey:
    """The parsed components of a ``{game_key}.l.{league_id}`` key."""

    game_key: str
    league_id: str

    def __str__(self) -> str:
        return f"{self.game_key}.l.{self.league_id}"


def build_league_key(game_key: str | int, league_id: str | int) -> str:
    """Compose a league key from its parts."""
    return f"{game_key}.l.{league_id}"


def build_team_key(league_key: str, team_id: str | int) -> str:
    """Compose a team key from a league key and a team id."""
    return f"{league_key}.t.{team_id}"


def build_player_key(game_key: str | int, player_id: str | int) -> str:
    """Compose a player key from a game key and a Yahoo player id."""
    return f"{game_key}.p.{player_id}"


def game_key_of(key: str) -> str:
    """Return the leading ``game_key`` of any composite Yahoo key.

    Args:
        key: A league, team, or player key (a bare game key is returned as-is).

    Returns:
        The game key segment, e.g. ``"449"`` for ``"449.l.123456.t.4"``.
    """
    return key.split(".", 1)[0] if key else ""


def league_key_of(team_key: str) -> str:
    """Return the ``league_key`` a ``team_key`` belongs to.

    Args:
        team_key: e.g. ``"449.l.123456.t.4"``.

    Returns:
        e.g. ``"449.l.123456"``. Returns the input unchanged if it has no
        ``.t.`` segment (i.e. it is already a league key).
    """
    marker = ".t."
    index = team_key.find(marker)
    return team_key[:index] if index != -1 else team_key


def split_league_key(league_key: str) -> LeagueKey:
    """Split ``"449.l.123456"`` into its :class:`LeagueKey` components.

    Raises:
        ValueError: If the key is not in ``{game_key}.l.{league_id}`` form.
    """
    parts = league_key.split(".")
    if len(parts) < 3 or parts[1] != "l":
        raise ValueError(
            f"Not a league key: {league_key!r} (expected '{{game_key}}.l.{{league_id}}')"
        )
    return LeagueKey(game_key=parts[0], league_id=parts[2])
