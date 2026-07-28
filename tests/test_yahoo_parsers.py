"""Tests for the Yahoo JSON normalization layer (M2).

Every test runs against a recorded-shape fixture in ``tests/fixtures`` — no
network, no live Yahoo call. The fixtures deliberately reproduce Yahoo's three
structural quirks (framework §2.3): count-indexed collections, positional
one-key-object arrays, and sub-resources whose encoding is inconsistent between
legs of the same response.
"""

from __future__ import annotations

import pytest

from fantasy_coach.clients import parsers
from fantasy_coach.clients.models import BENCH_POSITIONS, STAT_ID_RECEPTIONS
from tests.conftest import load_fixture


# -- structural primitives ---------------------------------------------------


def test_unwrap_requires_fantasy_content():
    with pytest.raises(parsers.YahooParseError):
        parsers.unwrap({"nope": 1})
    with pytest.raises(parsers.YahooParseError):
        parsers.unwrap({"fantasy_content": "not an object"})


def test_iter_collection_walks_numeric_keys_in_order():
    node = {"10": "k", "2": "c", "0": "a", "count": 3}
    assert list(parsers.iter_collection(node)) == ["a", "c", "k"]


def test_iter_collection_ignores_non_index_siblings():
    """``coverage_type``/``week`` sit alongside the indices on roster nodes."""
    node = {"0": "a", "count": 1, "coverage_type": "week", "week": "1"}
    assert list(parsers.iter_collection(node)) == ["a"]


def test_iter_collection_trusts_keys_over_a_lying_count():
    """Yahoo's ``count`` disagrees with reality on some final pages."""
    node = {"0": "a", "1": "b", "count": 99}
    assert list(parsers.iter_collection(node)) == ["a", "b"]


def test_iter_collection_passes_through_lists_and_ignores_junk():
    assert list(parsers.iter_collection(["a", "b"])) == ["a", "b"]
    assert list(parsers.iter_collection(None)) == []
    assert list(parsers.iter_collection("string")) == []


def test_collection_count_falls_back_to_member_count():
    assert parsers.collection_count({"0": "a", "count": "7"}) == 7
    assert parsers.collection_count({"0": "a", "1": "b"}) == 2


def test_collapse_flattens_positional_and_nested_shapes():
    assert parsers.collapse([{"a": 1}, {"b": 2}]) == {"a": 1, "b": 2}
    # The [[kv list], {sub}] shape Yahoo uses for teams and players.
    assert parsers.collapse([[{"a": 1}, {"b": 2}], {"sub": 3}]) == {
        "a": 1, "b": 2, "sub": 3
    }
    # Bare empty lists are Yahoo's placeholder for fields it skipped.
    assert parsers.collapse([{"a": 1}, [], {"b": 2}]) == {"a": 1, "b": 2}
    assert parsers.collapse({"a": 1}) == {"a": 1}
    assert parsers.collapse(None) == {}


@pytest.mark.parametrize(
    "value,expected",
    [("12", 12), (12, 12), (" 7 ", 7), ("", None), (None, None), ("x", None)],
)
def test_int_coercion(value, expected):
    assert parsers._int(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [("12.5", 12.5), (3, 3.0), ("-", None), ("", None), ("x", None)],
)
def test_float_coercion(value, expected):
    """``"-"`` is Yahoo's placeholder for a stat with no value yet."""
    assert parsers._float(value) == expected


@pytest.mark.parametrize(
    "value,expected",
    [("1", True), (1, True), ("0", False), (0, False), ("", False), (True, True)],
)
def test_bool_coercion(value, expected):
    assert parsers._bool(value) is expected


# -- game / league discovery -------------------------------------------------


def test_parse_game_reads_current_season_key():
    game = parsers.parse_game(load_fixture("game_nfl"))
    assert game.game_key == "449"
    assert game.season == "2026"
    assert game.code == "nfl"
    assert game.is_current is True


def test_parse_game_requires_a_game_key():
    with pytest.raises(parsers.YahooParseError):
        parsers.parse_game({"fantasy_content": {"game": [{}]}})


def test_parse_user_leagues_walks_users_games_leagues():
    """The deepest nesting Yahoo produces: three stacked count-dicts."""
    leagues = parsers.parse_user_leagues(load_fixture("user_leagues"))
    assert [lg.league_key for lg in leagues] == [
        "449.l.123456",
        "449.l.987654",
        "423.l.111222",
    ]


def test_parse_user_leagues_inherits_season_and_game_key_from_parent_game():
    """Each league must carry the game it was nested under, not a guess."""
    leagues = {lg.league_key: lg for lg in
               parsers.parse_user_leagues(load_fixture("user_leagues"))}

    current = leagues["449.l.123456"]
    assert current.season == "2026"
    assert current.game_key == "449"
    assert current.num_teams == 12
    assert current.draft_status == "predraft"

    prior = leagues["423.l.111222"]
    assert prior.season == "2025"
    assert prior.game_key == "423"
    assert prior.draft_status == "postdraft"


# -- league settings ---------------------------------------------------------


def test_parse_settings_reads_waiver_and_playoff_config():
    settings = parsers.parse_league_settings(load_fixture("league_settings"))
    assert settings.league_key == "449.l.123456"
    assert settings.draft_type == "live"
    assert settings.is_auction_draft is False
    assert settings.uses_playoff is True
    assert settings.playoff_start_week == 15
    assert settings.num_playoff_teams == 6
    assert settings.num_playoff_consolation_teams == 4
    assert settings.waiver_type == "FR"
    assert settings.waiver_rule == "gametime"
    assert settings.uses_faab is True
    assert settings.max_teams == 12
    assert settings.trade_end_date == "2026-11-27"
    assert settings.uses_negative_points is True


def test_parse_settings_reads_roster_slots():
    settings = parsers.parse_league_settings(load_fixture("league_settings"))
    assert settings.starting_slots() == {
        "QB": 1, "RB": 2, "WR": 2, "TE": 1, "W/R/T": 1, "K": 1, "DEF": 1,
    }
    assert settings.bench_slots == 6
    assert settings.injury_slots == 2
    assert settings.roster_size == 17
    assert all(pos in BENCH_POSITIONS for pos in ("BN", "IR"))


def test_parse_settings_joins_stat_categories_to_modifiers():
    """Yahoo ships names and point values as two lists keyed by stat_id."""
    settings = parsers.parse_league_settings(load_fixture("league_settings"))
    by_id = {cat.stat_id: cat for cat in settings.stat_categories}

    assert by_id[4].name == "Passing Yards"
    assert by_id[4].value == 0.04
    assert by_id[5].display_name == "Pass TD"
    assert by_id[5].value == 4.0
    assert by_id[6].value == -1.0  # interceptions cost points
    assert settings.points_for(10) == 6.0  # rushing TD
    assert settings.points_for(999) == 0.0  # unscored stat


def test_parse_settings_keeps_modifiers_with_no_matching_category():
    """A modifier Yahoo never defined a category for still scores points."""
    settings = parsers.parse_league_settings(load_fixture("league_settings"))
    modifiers = settings.stat_modifiers()
    assert modifiers[98] == 5.0
    assert 98 in {cat.stat_id for cat in settings.stat_categories}


def test_half_ppr_league_is_detected():
    settings = parsers.parse_league_settings(load_fixture("league_settings"))
    assert settings.points_per_reception == 0.5
    assert settings.ppr_type == "half_ppr"
    assert settings.stat_modifiers()[STAT_ID_RECEPTIONS] == 0.5
    assert settings.is_superflex is False


def test_superflex_full_ppr_league_is_detected():
    """A Q/W/R/T slot moves QB replacement level — M4 must see it (§4.2)."""
    settings = parsers.parse_league_settings(
        load_fixture("league_settings_superflex")
    )
    assert settings.ppr_type == "ppr"
    assert settings.points_per_reception == 1.0
    assert settings.is_superflex is True
    assert [slot.position for slot in settings.flex_slots()] == ["Q/W/R/T"]
    assert settings.is_auction_draft is True
    assert settings.uses_faab is False
    assert settings.injury_slots == 0


def test_roster_position_flex_helpers():
    settings = parsers.parse_league_settings(load_fixture("league_settings"))
    flex = settings.flex_slots()[0]
    assert flex.position == "W/R/T"
    assert flex.is_flex is True
    assert flex.flex_positions == ["W", "R", "T"]

    qb = next(p for p in settings.roster_positions if p.position == "QB")
    assert qb.is_flex is False
    assert qb.flex_positions == ["QB"]


# -- teams -------------------------------------------------------------------


def test_parse_teams_flattens_the_double_nested_field_bag():
    teams = parsers.parse_league_teams(load_fixture("league_teams"))
    assert [t.team_key for t in teams] == [
        "449.l.123456.t.1", "449.l.123456.t.2", "449.l.123456.t.3",
    ]
    first = teams[0]
    assert first.name == "Gridiron Gurus"
    assert first.team_id == "1"
    assert first.faab_balance == 87
    assert first.waiver_priority == 1
    assert first.number_of_moves == 4
    assert first.number_of_trades == 1


def test_parse_teams_marks_only_the_logged_in_users_team():
    """Yahoo emits ``is_owned_by_current_login`` only on your own team."""
    teams = parsers.parse_league_teams(load_fixture("league_teams"))
    owned = [t.team_key for t in teams if t.is_owned_by_current_login]
    assert owned == ["449.l.123456.t.1"]


def test_parse_teams_reads_managers():
    teams = parsers.parse_league_teams(load_fixture("league_teams"))
    manager = teams[0].managers[0]
    assert manager.nickname == "Sam"
    assert manager.manager_id == "1"
    assert manager.guid == "GUID1"


# -- players -----------------------------------------------------------------


def test_parse_players_reads_identity_and_positions():
    players = parsers.parse_players(load_fixture("league_players_page1"))
    assert len(players) == 25

    chase = players[0]
    assert chase.player_key == "449.p.31883"
    assert chase.player_id == "31883"
    assert chase.full_name == "Ja'Marr Chase"
    assert chase.first_name == "Ja'Marr"
    assert chase.editorial_team_abbr == "Cin"
    assert chase.primary_position == "WR"
    assert chase.eligible_positions == ["WR", "W/R/T"]
    assert chase.bye_week == 10


def test_parse_players_reads_percent_owned_and_draft_analysis():
    """Ownership is the waiver signal; ADP feeds M5's survival model (§4.3)."""
    players = parsers.parse_players(load_fixture("league_players_page1"))
    chase = players[0]
    assert chase.percent_owned == 100.0
    assert chase.percent_owned_delta == 0.0
    assert chase.average_draft_pick == 1.4
    assert chase.draft_analysis is not None
    assert chase.draft_analysis.percent_drafted == 1.0
    assert chase.draft_analysis.average_cost == 68.6


def test_parse_players_reads_yahoo_ranks():
    players = parsers.parse_players(load_fixture("league_players_page1"))
    chase = players[0]
    assert {r.rank_type for r in chase.player_ranks} == {"PR", "PS"}
    assert chase.rank("PR") == 1
    assert chase.rank("PS") == 3
    assert chase.rank("NOPE") is None


def test_parse_players_reads_injury_status():
    players = parsers.parse_players(load_fixture("league_players_page1"))
    nabers = next(p for p in players if p.full_name == "Malik Nabers")
    assert nabers.status == "Q"
    assert nabers.status_full == "Questionable"
    assert nabers.injury_note == "Hamstring"

    collins = next(p for p in players if p.full_name == "Nico Collins")
    assert collins.status == "O"
    assert collins.on_disabled_list is True


def test_parse_players_handles_team_defenses():
    """DSTs aren't players — §3.2 maps them by team code instead."""
    players = parsers.parse_players(load_fixture("league_players_page2"))
    dst = next(p for p in players if p.primary_position == "DEF")
    assert dst.full_name == "San Francisco"
    assert dst.position_type == "DT"
    assert dst.identity().is_defense is True


def test_parse_players_empty_final_page():
    """``{"players": {"count": 0}}`` is what ends pagination."""
    assert parsers.parse_players(load_fixture("league_players_empty")) == []


def test_player_identity_carries_everything_m3_crosswalks_on():
    """M3 joins on yahoo_id, falling back to (name, position, team) — §3.2."""
    players = parsers.parse_players(load_fixture("league_players_page1"))
    identity = players[0].identity()
    assert identity.yahoo_player_id == "31883"
    assert identity.yahoo_player_key == "449.p.31883"
    assert identity.full_name == "Ja'Marr Chase"
    assert identity.team_abbr == "Cin"
    assert identity.position == "WR"
    assert identity.eligible_positions == ("WR", "W/R/T")
    assert identity.bye_week == 10
    assert identity.is_defense is False


# -- rosters -----------------------------------------------------------------


def test_parse_team_roster_reads_coverage_and_slots():
    roster = parsers.parse_team_roster(load_fixture("team_roster"))
    assert roster.team_key == "449.l.123456.t.1"
    assert roster.week == 1
    assert roster.coverage_type == "week"
    assert roster.is_editable is True
    assert len(roster.slots) == 10
    assert len(roster.players) == 10


def test_parse_team_roster_splits_starters_from_bench():
    roster = parsers.parse_team_roster(load_fixture("team_roster"))
    assert [s.selected_position for s in roster.starters] == [
        "QB", "RB", "RB", "WR", "WR", "TE", "W/R/T", "DEF",
    ]
    assert [s.selected_position for s in roster.bench] == ["BN", "IR"]
    assert roster.bench[1].player.full_name == "Nico Collins"


def test_roster_flex_slot_is_flagged_on_the_player():
    roster = parsers.parse_team_roster(load_fixture("team_roster"))
    flex = next(s for s in roster.slots if s.selected_position == "W/R/T")
    assert flex.player.full_name == "De'Von Achane"
    assert flex.player.selected_position_is_flex is True


# -- draft results -----------------------------------------------------------


def test_parse_draft_results_sorts_by_pick():
    """The fixture is deliberately out of order; the board must be in order."""
    picks = parsers.parse_draft_results(load_fixture("draft_results"))
    assert [p.pick for p in picks] == list(range(1, 16))
    assert picks[0].round == 1
    assert picks[0].team_key == "449.l.123456.t.1"
    assert picks[0].player_key == "449.p.31883"


def test_parse_draft_results_flags_unmade_picks():
    """A live draft returns the whole board with future picks blank (§7)."""
    picks = parsers.parse_draft_results(load_fixture("draft_results"))
    made = [p for p in picks if p.is_made]
    assert len(made) == 13
    assert [p.pick for p in picks if not p.is_made] == [14, 15]


def test_draft_pick_exposes_bare_player_id_for_crosswalking():
    picks = parsers.parse_draft_results(load_fixture("draft_results"))
    assert picks[0].player_id == "31883"
    assert picks[-1].player_id == ""


# -- transactions ------------------------------------------------------------


def test_parse_transactions_reads_add_drop_with_faab_bid():
    transactions = parsers.parse_transactions(load_fixture("transactions"))
    add_drop = transactions[0]
    assert add_drop.type == "add/drop"
    assert add_drop.status == "successful"
    assert add_drop.faab_bid == 17
    assert add_drop.timestamp == 1758153600
    assert add_drop.datetime_utc is not None
    assert add_drop.datetime_utc.year == 2025


def test_transaction_data_parses_as_both_object_and_wrapped_list():
    """The add leg is a bare object, the drop leg the same object in a list.

    This is the single nastiest inconsistency in Yahoo's JSON; both legs must
    come out identically shaped.
    """
    transactions = parsers.parse_transactions(load_fixture("transactions"))
    add_drop = transactions[0]

    added = add_drop.added
    dropped = add_drop.dropped
    assert [p.name for p in added] == ["Cam Skattebo"]
    assert [p.name for p in dropped] == ["Tony Pollard"]

    assert added[0].source_type == "waivers"
    assert added[0].destination_type == "team"
    assert added[0].destination_team_key == "449.l.123456.t.1"

    assert dropped[0].source_type == "team"
    assert dropped[0].source_team_key == "449.l.123456.t.1"
    assert dropped[0].destination_type == "waivers"


def test_parse_transactions_reads_trade_sides():
    transactions = parsers.parse_transactions(load_fixture("transactions"))
    trade = transactions[1]
    assert trade.type == "trade"
    assert trade.trader_team_key == "449.l.123456.t.2"
    assert trade.tradee_team_key == "449.l.123456.t.3"
    assert [p.name for p in trade.players] == ["Trey McBride", "Sam LaPorta"]
    assert all(p.movement_type == "trade" for p in trade.players)


def test_parse_transactions_captures_player_ids_for_crosswalking():
    transactions = parsers.parse_transactions(load_fixture("transactions"))
    leg = transactions[2].players[0]
    assert leg.player_key == "449.p.32710"
    assert leg.player_id == "32710"
    assert leg.editorial_team_abbr == "Ten"
    assert leg.display_position == "TE"
    assert leg.source_type == "freeagents"


# -- matchups ----------------------------------------------------------------


def test_parse_scoreboard_digs_teams_out_of_the_numeric_key():
    """Each matchup hides its team pair under a ``"0"`` key inside itself."""
    matchups = parsers.parse_scoreboard(load_fixture("scoreboard_week1"))
    assert len(matchups) == 2

    first = matchups[0]
    assert first.week == 1
    assert first.week_start == "2026-09-10"
    assert first.week_end == "2026-09-15"
    assert first.status == "postevent"
    assert first.is_playoffs is False
    assert first.winner_team_key == "449.l.123456.t.1"
    assert first.team_keys == ["449.l.123456.t.1", "449.l.123456.t.2"]


def test_parse_scoreboard_reads_actual_and_projected_points():
    """M11's calibration loop joins these two per week (§4.5)."""
    matchups = parsers.parse_scoreboard(load_fixture("scoreboard_week1"))
    home, away = matchups[0].teams
    assert (home.name, home.points, home.projected_points) == (
        "Gridiron Gurus", 121.5, 118.2
    )
    assert (away.points, away.projected_points) == (98.3, 105.7)


def test_matchup_opponent_lookup():
    matchups = parsers.parse_scoreboard(load_fixture("scoreboard_week1"))
    opponent = matchups[0].opponent_of("449.l.123456.t.1")
    assert opponent is not None
    assert opponent.team_key == "449.l.123456.t.2"
    assert matchups[0].opponent_of("449.l.123456.t.9") is None
