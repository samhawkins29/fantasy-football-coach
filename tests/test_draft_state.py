"""DraftState — drafted-set rebuild (undo-safe), keepers, unmapped ids."""

from __future__ import annotations

import logging

import pytest

from fantasy_coach.draft.state import DraftState, snake_team_for_pick
from tests.conftest import DRAFT_LEAGUE_KEY, make_draft_pool, make_pick

MY_TEAM = f"{DRAFT_LEAGUE_KEY}.t.1"
OPP_TEAM = f"{DRAFT_LEAGUE_KEY}.t.2"


@pytest.fixture
def state() -> DraftState:
    players, _ = make_draft_pool()
    return DraftState(players, league_key=DRAFT_LEAGUE_KEY, my_team_key=MY_TEAM)


# -- resolution ---------------------------------------------------------------


def test_resolves_yahoo_id_to_canonical(state):
    assert state.resolve_raw_id("201") == "R1"


def test_resolves_canonical_id_directly_for_offline_stores(state):
    # W6 has no yahoo id; simulation picks carry the canonical id instead.
    assert state.resolve_raw_id("W6") == "W6"


def test_unknown_id_resolves_to_none(state):
    assert state.resolve_raw_id("9999") is None


# -- rebuild (the undo-safety core) -------------------------------------------


def test_rebuild_builds_drafted_set_from_made_picks_only(state):
    from fantasy_coach.clients.models import DraftPick

    picks = [
        make_pick(1, OPP_TEAM, "201"),          # R1 via yahoo id
        make_pick(2, MY_TEAM, "W6"),            # canonical fallback
        DraftPick(pick=3, round=2),             # not yet made — ignored
    ]
    state.rebuild(picks)
    assert state.drafted_canonical_ids == {"R1", "W6"}
    assert state.pick_count == 2


def test_rebuild_is_not_append_undo_returns_player_to_pool(state):
    state.rebuild([make_pick(1, OPP_TEAM, "201"), make_pick(2, MY_TEAM, "202")])
    assert state.drafted_canonical_ids == {"R1", "R2"}

    # Commissioner undoes pick 2: it simply vanishes from the next poll.
    state.rebuild([make_pick(1, OPP_TEAM, "201")])
    assert state.drafted_canonical_ids == {"R1"}
    assert state.my_canonical_ids() == []


def test_rebuild_rederives_unmapped_picks_each_time(state):
    state.rebuild([make_pick(1, OPP_TEAM, "9999")])
    assert state.unmapped == {"9999": 1}
    state.rebuild([])  # the bogus pick was corrected
    assert state.unmapped == {}


# -- my roster ----------------------------------------------------------------


def test_my_picks_filtered_by_team_key_in_pick_order(state):
    state.rebuild(
        [
            make_pick(1, OPP_TEAM, "201"),
            make_pick(2, MY_TEAM, "301"),
            make_pick(3, MY_TEAM, "401"),
        ]
    )
    assert state.my_canonical_ids() == ["W1", "T1"]
    assert [rp.canonical_id for rp in state.my_picks] == ["W1", "T1"]


# -- keepers ------------------------------------------------------------------


def test_keeper_seeding_removes_players_and_survives_rebuild(state):
    state.seed_keepers(OPP_TEAM, ["201"])
    state.seed_keepers(MY_TEAM, ["102"])
    state.rebuild([])
    assert state.drafted_canonical_ids == {"R1", "Q2"}
    assert state.my_canonical_ids() == ["Q2"]  # my keeper is on my roster

    state.rebuild([make_pick(1, MY_TEAM, "301")])
    assert state.drafted_canonical_ids == {"R1", "Q2", "W1"}
    assert state.my_canonical_ids() == ["Q2", "W1"]  # keepers first


def test_unmapped_keeper_counts_toward_my_roster(state):
    state.seed_keepers(MY_TEAM, ["9999"])
    state.rebuild([])
    assert state.my_unmapped_count() == 1
    assert "9999" in state.unmapped


# -- unmapped picks -----------------------------------------------------------


def test_unmapped_pick_counts_excludes_by_raw_id_and_logs_once(state, caplog):
    picks = [make_pick(1, MY_TEAM, "9999")]
    with caplog.at_level(logging.WARNING, logger="fantasy_coach.draft.state"):
        state.rebuild(picks)
        state.rebuild(picks)  # second rebuild must not re-log
    warnings = [r for r in caplog.records if "unmapped" in r.message]
    assert len(warnings) == 1
    assert state.pick_count == 1                 # the pick still counts
    assert state.drafted_raw_ids == {"9999"}     # excluded by raw id
    assert state.drafted_canonical_ids == set()  # but has no board identity
    assert state.my_unmapped_count() == 1        # and occupies one of my slots


# -- snake prediction ---------------------------------------------------------


def test_snake_team_for_pick_reverses_each_round():
    order = ["t1", "t2", "t3"]
    picks = [snake_team_for_pick(order, n) for n in range(1, 8)]
    assert picks == ["t1", "t2", "t3", "t3", "t2", "t1", "t1"]


def test_snake_team_unknown_without_round1_order():
    assert snake_team_for_pick([], 5) is None
