"""Tests for the name / team / position normalizers (M3, framework §3.2)."""

from __future__ import annotations

import pytest

from fantasy_coach.ingest.names import (
    CANONICAL_TEAMS,
    clean_name,
    is_defense_position,
    match_key,
    normalize_position,
    normalize_team,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Amon-Ra St. Brown", "amon ra st brown"),
        ("Michael Pittman Jr.", "michael pittman"),
        ("Ken Walker III", "ken walker"),
        ("D.J. Moore", "d j moore"),
        ("Ka'imi Fairbairn", "kaimi fairbairn"),
        ("  Patrick   Mahomes  ", "patrick mahomes"),
        ("José Ramírez", "jose ramirez"),
        ("A.J. Brown", "a j brown"),
        ("", ""),
        (None, ""),
    ],
)
def test_clean_name(raw, expected):
    assert clean_name(raw) == expected


def test_clean_name_does_not_strip_only_token():
    # A single-token name that *is* a suffix must not vanish.
    assert clean_name("V") == "v"


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("KCC", "KC"),
        ("SFO", "SF"),
        ("GBP", "GB"),
        ("LVR", "LV"),
        ("Was", "WAS"),
        ("Jax", "JAX"),
        ("OAK", "LV"),   # relocation
        ("SD", "LAC"),   # relocation
        ("STL", "LAR"),  # relocation
        ("KC", "KC"),    # already canonical
        ("kc", "KC"),    # case-insensitive
    ],
)
def test_normalize_team(raw, expected):
    assert normalize_team(raw) == expected
    assert expected in CANONICAL_TEAMS


@pytest.mark.parametrize("raw", ["", None, "FA", "N/A", "XYZ", "--"])
def test_normalize_team_unknown_is_blank(raw):
    assert normalize_team(raw) == ""


@pytest.mark.parametrize(
    "raw, expected",
    [("qb", "QB"), ("DEF", "DEF"), ("DST", "DEF"), ("D/ST", "DEF"), ("", "")],
)
def test_normalize_position(raw, expected):
    assert normalize_position(raw) == expected


@pytest.mark.parametrize("raw, expected", [("DEF", True), ("DST", True), ("QB", False), ("", False)])
def test_is_defense_position(raw, expected):
    assert is_defense_position(raw) is expected


def test_match_key_normalizes_all_three_fields():
    # A Yahoo-style (name w/ suffix, team code) and an nflverse-style row for the
    # same player produce identical keys.
    assert match_key("Ken Walker III", "rb", "SEA") == ("ken walker", "RB", "SEA")
    assert match_key("Patrick Mahomes", "QB", "KCC") == ("patrick mahomes", "QB", "KC")
