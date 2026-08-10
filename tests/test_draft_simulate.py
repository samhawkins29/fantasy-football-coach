"""Simulation source + scripted-draft generator."""

from __future__ import annotations

import pytest

from fantasy_coach.draft.simulate import SimulatedPickSource, script_draft, sim_team_names
from tests.conftest import DRAFT_LEAGUE_KEY, make_pick


@pytest.fixture
def script(draft_store, draft_settings):
    return script_draft(draft_store, draft_settings)


# -- script generation --------------------------------------------------------


def test_script_covers_every_draftable_round_without_duplicates(script):
    # 2 teams x (9 roster spots - 1 IR) = 16 picks.
    assert len(script) == 16
    assert [p.pick for p in script] == list(range(1, 17))
    ids = [p.player_id for p in script]
    assert len(set(ids)) == 16  # nobody drafted twice


def test_script_is_a_snake(script):
    t1, t2 = script[0].team_key, script[1].team_key
    assert t1 != t2
    assert script[2].team_key == t2  # round 2 reverses
    assert script[3].team_key == t1


def test_script_drafts_market_order_and_respects_caps(script, draft_store):
    # ADP 1 and 2 are R1/R2 (yahoo ids 201/202) — the first two off the board.
    assert script[0].player_id == "201"
    assert script[1].player_id == "202"
    for team in {p.team_key for p in script}:
        rows = draft_store.sql(
            "SELECT position, COUNT(*) n FROM players WHERE yahoo_id IN ({}) "
            "OR canonical_id IN ({}) GROUP BY position".format(
                ",".join("?" * 8), ",".join("?" * 8)
            ),
            [p.player_id for p in script if p.team_key == team] * 2,
        )
        counts = {r["position"]: r["n"] for r in rows}
        assert counts.get("QB", 0) <= 2
        assert counts.get("TE", 0) <= 2


def test_script_requires_a_warmed_board(draft_settings):
    from fantasy_coach.store import CoachStore

    with CoachStore(":memory:") as empty:
        with pytest.raises(ValueError, match="warm the store"):
            script_draft(empty, draft_settings)


# -- the source ---------------------------------------------------------------


def test_source_reveals_picks_incrementally_with_yahoo_shape():
    script = [make_pick(n, "t.1", str(200 + n)) for n in range(1, 4)]
    source = SimulatedPickSource(script, picks_per_poll=1)

    first = source.fetch()
    assert len(first) == 3                      # full list, like Yahoo
    assert all(not p.is_made for p in first)    # nothing made yet

    second = source.fetch()
    assert [p.is_made for p in second] == [True, False, False]
    assert second[1].player_key == ""           # unmade picks are blanked

    source.fetch()
    assert [p.is_made for p in source.fetch()] == [True, True, True]
    assert source.exhausted


def test_source_rewind_unmakes_picks():
    script = [make_pick(n, "t.1", str(200 + n)) for n in range(1, 4)]
    # picks_per_poll=0 freezes the room so only rewind moves the state
    source = SimulatedPickSource(script, picks_per_poll=0, start_made=2)
    assert sum(p.is_made for p in source.fetch()) == 2
    source.rewind(1)
    assert sum(p.is_made for p in source.fetch()) == 1


def test_sim_team_names_marks_mine():
    names = sim_team_names(DRAFT_LEAGUE_KEY, 3, my_slot=2)
    assert names[f"{DRAFT_LEAGUE_KEY}.t.2"] == "You"
    assert names[f"{DRAFT_LEAGUE_KEY}.t.1"] == "Team 1"
