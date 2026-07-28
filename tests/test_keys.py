"""Tests for Yahoo composite-key construction and parsing (framework §2.3).

Pure string manipulation — no I/O, no network.
"""

from __future__ import annotations

import pytest

from fantasy_coach.clients.keys import (
    LeagueKey,
    build_league_key,
    build_player_key,
    build_team_key,
    game_key_of,
    league_key_of,
    split_league_key,
)


def test_build_keys():
    league_key = build_league_key("449", "123456")
    assert league_key == "449.l.123456"
    assert build_team_key(league_key, 4) == "449.l.123456.t.4"
    assert build_player_key("449", 31883) == "449.p.31883"


def test_player_keys_hang_off_the_game_not_the_league():
    """The same player has one key across every league in a season — §3.2."""
    assert build_player_key(game_key_of("449.l.123456"), "31883") == "449.p.31883"


@pytest.mark.parametrize(
    "key,expected",
    [
        ("449", "449"),
        ("449.l.123456", "449"),
        ("449.l.123456.t.4", "449"),
        ("449.p.31883", "449"),
        ("", ""),
    ],
)
def test_game_key_of(key, expected):
    assert game_key_of(key) == expected


def test_league_key_of_a_team_key():
    assert league_key_of("449.l.123456.t.4") == "449.l.123456"


def test_league_key_of_is_idempotent_on_a_league_key():
    assert league_key_of("449.l.123456") == "449.l.123456"


def test_split_league_key():
    parsed = split_league_key("449.l.123456")
    assert parsed == LeagueKey(game_key="449", league_id="123456")
    assert str(parsed) == "449.l.123456"


@pytest.mark.parametrize("bad", ["449", "449.t.4", "nonsense", ""])
def test_split_league_key_rejects_non_league_keys(bad):
    with pytest.raises(ValueError):
        split_league_key(bad)
