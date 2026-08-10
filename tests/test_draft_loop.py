"""DraftLoop integration — the poll→rebuild→recommend cycle, fully offline.

Uses the conftest draft pool (hand-computable VORPs: R1..R6 = 100/80/60/40/20/0,
W1 45, T1 70, Q1 20 …) so every recommendation shift asserted here is exact.
"""

from __future__ import annotations

import pytest

from fantasy_coach.draft.loop import DraftLoop
from fantasy_coach.draft.simulate import SimulatedPickSource
from tests.conftest import DRAFT_LEAGUE_KEY, FakeClock, make_pick

MY_TEAM = f"{DRAFT_LEAGUE_KEY}.t.1"
OPP_TEAM = f"{DRAFT_LEAGUE_KEY}.t.2"


class ListSource:
    """A pick source the test mutates directly."""

    def __init__(self, picks=None):
        self.picks = list(picks or [])

    def fetch(self):
        return list(self.picks)


def make_loop(draft_store, draft_settings, source, **kwargs):
    clock = kwargs.pop("clock", FakeClock())
    kwargs.setdefault("my_team_key", MY_TEAM)
    kwargs.setdefault("mode", "simulation")
    return (
        DraftLoop(
            draft_store,
            draft_settings,
            source,
            time_func=clock.time,
            sleep_func=clock.sleep,
            **kwargs,
        ),
        clock,
    )


# -- first poll ---------------------------------------------------------------


def test_initial_recommendation_is_best_overall(draft_store, draft_settings):
    loop, _ = make_loop(draft_store, draft_settings, ListSource())
    snap = loop.poll_once()
    assert loop.recommendation.player.entry.canonical_id == "R1"
    assert snap["recommendation"]["name"] == "Rusher One"
    assert snap["available_count"] == 19
    assert snap["draft"]["pick_count"] == 0


# -- availability filtering + pool shrink -------------------------------------


def test_drafted_players_leave_the_available_board(draft_store, draft_settings):
    source = ListSource([make_pick(1, OPP_TEAM, "201")])  # R1 gone
    loop, _ = make_loop(draft_store, draft_settings, source)
    snap = loop.poll_once()
    names = [p["canonical_id"] for p in snap["available"]]
    assert "R1" not in names
    assert loop.recommendation.player.entry.canonical_id == "R2"


def test_rb_run_shifts_recommendation_to_next_position(draft_store, draft_settings):
    # Opponent hoovers up the top four RBs: remaining RB VORPs (20, 0) can't
    # compete with T1 (70), so the board must pivot off RB entirely.
    source = ListSource(
        [make_pick(n, OPP_TEAM, raw) for n, raw in enumerate(("201", "202", "203", "204"), 1)]
    )
    loop, _ = make_loop(draft_store, draft_settings, source)
    loop.poll_once()
    rec = loop.recommendation.player.entry
    assert rec.canonical_id == "T1"
    assert rec.position == "TE"


def test_baselines_shift_as_the_pool_drains(draft_store, draft_settings):
    loop, _ = make_loop(draft_store, draft_settings, ListSource())
    loop.poll_once()
    assert loop.board.baselines["QB"] == 180.0  # Q3 is replacement

    # Q1+Q2 drafted -> only Q3/Q4 remain; league demand (2 starters) now
    # exhausts the pool and replacement drops to Q4's 170.
    loop2, _ = make_loop(
        draft_store,
        draft_settings,
        ListSource([make_pick(1, OPP_TEAM, "101"), make_pick(2, OPP_TEAM, "102")]),
    )
    loop2.poll_once()
    assert loop2.board.baselines["QB"] == 170.0


# -- undo through the loop ----------------------------------------------------


def test_undo_returns_player_to_pool_and_store(draft_store, draft_settings):
    source = ListSource([make_pick(1, OPP_TEAM, "201")])
    loop, _ = make_loop(draft_store, draft_settings, source, record_to_store=True)
    loop.poll_once()
    assert loop.recommendation.player.entry.canonical_id == "R2"
    assert len(draft_store.sql("SELECT * FROM draft_picks")) == 1

    source.picks = []  # the pick is undone in the room
    snap = loop.poll_once()
    assert loop.recommendation.player.entry.canonical_id == "R1"
    assert any(p["canonical_id"] == "R1" for p in snap["available"])
    assert len(draft_store.sql("SELECT * FROM draft_picks")) == 0  # clear+record


# -- my picks: roster + need weighting ----------------------------------------


def test_my_picks_fill_roster_and_downweight_the_position(draft_store, draft_settings):
    source = ListSource(
        [
            make_pick(1, MY_TEAM, "201"),  # R1 -> RB slot
            make_pick(2, MY_TEAM, "202"),  # R2 -> RB slot (dedicated full)
            make_pick(3, MY_TEAM, "203"),  # R3 -> flex (RB now depth-only)
        ]
    )
    loop, _ = make_loop(draft_store, draft_settings, source)
    snap = loop.poll_once()

    filled = [s for s in snap["roster"] if s["player"] is not None]
    assert [s["label"] for s in filled] == ["RB", "RB", "W/R/T"]
    assert [s["player"]["name"] for s in filled][:2] == ["Rusher One", "Rusher Two"]

    by_id = {p["canonical_id"]: p for p in snap["available"]}
    assert by_id["R4"]["need"] == "depth"
    assert by_id["R4"]["weight"] == 0.55
    assert by_id["W1"]["need"] == "starter"
    # W1 (45 * 1.0) must outrank R4 (40 * .55) despite similar raw VORP …
    order = [p["canonical_id"] for p in snap["available"]]
    assert order.index("W1") < order.index("R4")
    # … while T1 (70, open TE slot) tops the whole board.
    assert snap["recommendation"]["canonical_id"] == "T1"


# -- keeper seeding -----------------------------------------------------------


def test_keepers_are_never_recommended_and_fill_my_roster(draft_store, draft_settings):
    loop, _ = make_loop(draft_store, draft_settings, ListSource())
    loop.seed_keepers(OPP_TEAM, ["201"])  # R1 kept by the opponent
    loop.seed_keepers(MY_TEAM, ["101"])   # Q1 kept by me
    snap = loop.poll_once()

    ids = {p["canonical_id"] for p in snap["available"]}
    assert "R1" not in ids and "Q1" not in ids
    assert snap["recommendation"]["canonical_id"] == "R2"
    qb_slot = next(s for s in snap["roster"] if s["label"] == "QB")
    assert qb_slot["player"]["name"] == "Quincy One"


# -- unmapped ids -------------------------------------------------------------


def test_unmapped_pick_is_counted_and_shown_but_board_survives(
    draft_store, draft_settings
):
    source = ListSource([make_pick(1, OPP_TEAM, "9999")])
    loop, _ = make_loop(draft_store, draft_settings, source)
    snap = loop.poll_once()
    assert snap["unmapped_count"] == 1
    assert snap["draft"]["pick_count"] == 1
    assert snap["available_count"] == 19  # nothing on the board matched the id
    assert snap["recent_picks"][0]["unmapped"] is True
    assert "9999" in snap["recent_picks"][0]["name"]


# -- staleness / freshness ----------------------------------------------------


def test_snapshot_flags_stale_when_polls_stop(draft_store, draft_settings):
    loop, clock = make_loop(draft_store, draft_settings, ListSource())
    loop.poll_once()
    assert loop.snapshot()["stale"] is False
    clock.advance(60)  # a minute with no successful poll
    snap = loop.snapshot()
    assert snap["stale"] is True
    assert snap["age_seconds"] == pytest.approx(60, abs=1)


def test_snapshot_stale_before_first_poll(draft_store, draft_settings):
    loop, _ = make_loop(draft_store, draft_settings, ListSource())
    assert loop.snapshot()["stale"] is True


# -- error resilience ---------------------------------------------------------


def test_run_survives_poll_errors_and_recovers(draft_store, draft_settings):
    calls = {"n": 0}

    class FlakySource:
        def fetch(self):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("yahoo hiccup")
            return []

    loop, _ = make_loop(draft_store, draft_settings, FlakySource())
    loop.run(max_polls=3)
    assert calls["n"] == 3           # the loop kept polling through the error
    assert loop.poll_count == 2      # two successful polls
    assert loop.snapshot()["error"] is None  # last poll succeeded


# -- clock / on-the-clock -----------------------------------------------------


def test_snake_prediction_after_round_one(draft_store, draft_settings):
    from fantasy_coach.clients.models import DraftPick

    script = [
        make_pick(1, MY_TEAM, "201"),
        make_pick(2, OPP_TEAM, "202"),
        DraftPick(pick=3, round=2),
        DraftPick(pick=4, round=2),
    ]
    loop, _ = make_loop(draft_store, draft_settings, ListSource(script))
    snap = loop.poll_once()
    d = snap["draft"]
    assert d["current_pick"] == 3 and d["round"] == 2
    assert d["on_the_clock"]["team_key"] == OPP_TEAM  # snake reverses
    assert d["my_next_pick"] == 4 and d["picks_until_mine"] == 1


def test_draft_complete_state(draft_store, draft_settings):
    script = [make_pick(1, MY_TEAM, "201"), make_pick(2, OPP_TEAM, "202")]
    loop, _ = make_loop(draft_store, draft_settings, ListSource(script))
    snap = loop.poll_once()
    assert snap["draft"]["complete"] is True
    assert snap["draft"]["on_the_clock"] is None


# -- full simulation end to end ----------------------------------------------


def test_simulated_draft_runs_to_completion(draft_store, draft_settings):
    from fantasy_coach.draft.simulate import script_draft

    script = script_draft(draft_store, draft_settings)
    source = SimulatedPickSource(script, picks_per_poll=1)
    loop, _ = make_loop(
        draft_store, draft_settings, source, my_team_key=f"{DRAFT_LEAGUE_KEY}.t.1"
    )

    seen_recs = []
    snap = loop.poll_once()
    while not snap["draft"]["complete"]:
        if snap["recommendation"]:
            seen_recs.append(snap["recommendation"]["canonical_id"])
        snap = loop.poll_once()

    assert snap["draft"]["pick_count"] == len(script) == 16
    # Pool drained pick by pick and the advice moved with it.
    assert len(set(seen_recs)) > 4
    assert seen_recs[0] == "R1"
    # My roster filled up through the same loop.
    my_players = [s for s in snap["roster"] if s["player"] is not None]
    assert len(my_players) == 8
