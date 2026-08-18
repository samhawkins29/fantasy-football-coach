"""Draft-survival probability (upgrade 2, framework §4.3).

Invariants:

* survival is 1.0 when you are on the clock, strictly non-increasing as your
  next pick moves further away, and non-decreasing in ADP (later-ADP players
  are safer);
* a player already past his ADP is not written off — the conditional handles
  it, and a far-stale ADP degrades to the flat per-pick hazard;
* the room's drift (picks running ahead of ADP) lowers survival; a positional
  run lowers survival for *that* position only;
* labels follow the probability cut-offs;
* the two-pick lookahead swaps only when the picture is clear-cut (candidate
  close in value, unlikely to survive; top very likely to) and never to a
  clearly worse player; the recommendation narrates the swap;
* the live loop stamps survival on every ranked player, updates it as picks
  land, and can predict my picks from a configured draft order before round
  one is observed.
"""

from __future__ import annotations

import pytest

from fantasy_coach.draft.recommend import (
    RankedPlayer,
    build_recommendation,
    compute_needs,
    lookahead_pick,
    rank_available,
    roster_slots,
)
from fantasy_coach.draft.survival import (
    LABEL_COIN_FLIP,
    LABEL_LIKELY,
    LABEL_SAFE,
    LABEL_TAKE_NOW,
    RUN_WINDOW,
    STALE_ADP_HAZARD,
    RoomState,
    Survival,
    adp_sigma,
    estimate_survival,
    room_state,
    survival_label,
    survival_probability,
)
from fantasy_coach.value.board import BoardEntry
from tests.conftest import DRAFT_LEAGUE_KEY, make_pick
from tests.test_draft_loop import ListSource, make_loop

MY_TEAM = f"{DRAFT_LEAGUE_KEY}.t.1"
OPP_TEAM = f"{DRAFT_LEAGUE_KEY}.t.2"


# -- the probability model ----------------------------------------------------


def test_on_the_clock_is_certain_and_survival_falls_with_distance():
    ps = [
        survival_probability(20.0, 3.0, current_pick=15, target_pick=t)
        for t in (15, 16, 18, 20, 24, 30)
    ]
    assert ps[0] == 1.0
    assert ps == sorted(ps, reverse=True)
    assert ps[-1] < 0.05


def test_later_adp_is_safer_at_the_same_pick():
    ps = [
        survival_probability(adp, adp_sigma(adp), current_pick=10, target_pick=21)
        for adp in (8.0, 15.0, 25.0, 60.0)
    ]
    assert ps == sorted(ps)
    assert ps[0] < 0.2 and ps[-1] > 0.95


def test_player_past_his_adp_still_gets_a_real_conditional():
    # ADP 5, σ 2.5, we're at pick 9 → he "should" be gone; the conditional
    # says each further pick is a fresh chance he goes, not "0".
    p = survival_probability(5.0, 2.5, current_pick=9, target_pick=11)
    assert 0.0 < p < 0.5
    # A stale ADP (many σ past) degrades to the flat per-pick hazard.
    far = survival_probability(5.0, 2.5, current_pick=60, target_pick=63)
    assert far == pytest.approx((1 - STALE_ADP_HAZARD) ** 3)


def test_adp_sigma_grows_with_adp_and_prefers_reported_stdev():
    assert adp_sigma(3.0) == 2.5  # floor
    assert adp_sigma(100.0) == pytest.approx(12.0)
    assert adp_sigma(100.0, stdev=4.0) == 4.0
    assert adp_sigma(100.0, stdev=1.0) == 2.5  # floored


def test_labels_follow_the_cutoffs():
    assert survival_label(0.1) == LABEL_TAKE_NOW
    assert survival_label(0.5) == LABEL_COIN_FLIP
    assert survival_label(0.75) == LABEL_LIKELY
    assert survival_label(0.95) == LABEL_SAFE
    assert survival_label(None) == ""


# -- the room -----------------------------------------------------------------


def test_drift_lowers_survival_and_is_clamped():
    # Every made pick had an ADP 5 later than the slot he went at: the room is
    # running 5 picks AHEAD of the market → drift −5 → everyone less safe.
    made = [(n, "RB", n + 5.0) for n in range(1, 13)]
    room = room_state(made, [("RB", 20.0), ("WR", 21.0)])
    assert room.drift == pytest.approx(-5.0)
    wild = room_state([(n, "RB", n + 40.0) for n in range(1, 13)], [("RB", 20.0)])
    assert wild.drift == -6.0  # clamped at DRIFT_MAX
    behind = room_state([(n, "RB", n - 3.0) for n in range(1, 13)], [("RB", 20.0)])
    assert behind.drift == pytest.approx(3.0)  # room behind the market → safer
    neutral = estimate_survival(
        [{"canonical_id": "x", "position": "RB", "adp": 20.0, "overall_rank": 5}],
        current_pick=13, my_next_pick=18, my_pick_after=30,
    )["x"]
    drifted = estimate_survival(
        [{"canonical_id": "x", "position": "RB", "adp": 20.0, "overall_rank": 5}],
        current_pick=13, my_next_pick=18, my_pick_after=30, room=room,
    )["x"]
    assert drifted.p_next < neutral.p_next
    assert drifted.p_after < neutral.p_after


def test_positional_run_lowers_that_position_only():
    # Market expects a WR/RB mix next; the room just took RB six of eight.
    available = [("RB", 9.0), ("WR", 10.0), ("WR", 11.0), ("RB", 12.0),
                 ("WR", 13.0), ("TE", 14.0), ("WR", 15.0), ("QB", 16.0)]
    made = [(n, "RB" if n != 3 and n != 6 else "WR", float(n)) for n in range(1, 9)]
    room = room_state(made, available, run_window=RUN_WINDOW)
    assert room.run_excess.get("RB", 0.0) > 0.5
    assert "WR" not in room.run_excess
    players = [
        {"canonical_id": "rb", "position": "RB", "adp": 14.0, "overall_rank": 5},
        {"canonical_id": "wr", "position": "WR", "adp": 14.0, "overall_rank": 6},
    ]
    calm = estimate_survival(players, current_pick=9, my_next_pick=14, my_pick_after=25)
    run = estimate_survival(players, current_pick=9, my_next_pick=14, my_pick_after=25, room=room)
    assert run["rb"].p_next < calm["rb"].p_next
    assert run["wr"].p_next == pytest.approx(calm["wr"].p_next)
    assert run["rb"].run_excess > 0 and run["wr"].run_excess == 0


def test_no_adp_falls_back_to_rank_with_wide_sigma():
    out = estimate_survival(
        [{"canonical_id": "u", "position": "WR", "adp": None, "overall_rank": 30}],
        current_pick=10, my_next_pick=15, my_pick_after=None,
    )["u"]
    assert out.source == "rank" and out.sigma > 10
    assert out.effective_adp == 39.0  # 30th best left ≈ 30 picks from pick 10
    assert out.p_after is None and out.p_next is not None
    top = estimate_survival(
        [{"canonical_id": "t", "position": "WR", "adp": None, "overall_rank": 1}],
        current_pick=10, my_next_pick=15, my_pick_after=None,
    )["t"]
    assert top.p_next < out.p_next  # the best player left is the least safe


def test_unknown_next_pick_gives_no_probability_but_keeps_the_estimate():
    out = estimate_survival(
        [{"canonical_id": "a", "position": "RB", "adp": 3.0, "overall_rank": 1}],
        current_pick=1, my_next_pick=None, my_pick_after=None,
    )["a"]
    assert out.p_next is None and out.label == ""


# -- the lookahead ------------------------------------------------------------


def _rp(name, pos, score, p_after, adp=None):
    e = BoardEntry(canonical_id=name, name=name, position=pos, vorp=score, adp=adp)
    sv = Survival(p_next=1.0, p_after=p_after, effective_adp=adp or 0.0, sigma=3.0)
    return RankedPlayer(entry=e, score=score, weight=1.0, need="starter", survival=sv)


def test_lookahead_swaps_to_the_player_who_wont_last():
    ranked = [
        _rp("Safe Star", "WR", 100.0, 0.95),   # will surely be there next round
        _rp("Hot Back", "RB", 92.0, 0.20),     # gone in a few picks
        _rp("Filler", "TE", 60.0, 0.9),
        _rp("Fallback", "QB", 50.0, 0.9),
    ]
    pick, swapped = lookahead_pick(ranked, picks_until_next=3)
    assert pick.entry.name == "Hot Back"
    assert swapped is not None and swapped.entry.name == "Safe Star"


def test_lookahead_never_swaps_to_a_clearly_worse_player():
    ranked = [
        _rp("Safe Star", "WR", 100.0, 0.95),
        _rp("Hot Back", "RB", 70.0, 0.10),  # < 80% of the top score
        _rp("Filler", "TE", 60.0, 0.9),
    ]
    pick, swapped = lookahead_pick(ranked, picks_until_next=3)
    assert pick.entry.name == "Safe Star" and swapped is None


def test_lookahead_holds_when_the_top_pick_is_also_at_risk():
    ranked = [
        _rp("Star", "WR", 100.0, 0.50),
        _rp("Back", "RB", 95.0, 0.20),
        _rp("Filler", "TE", 60.0, 0.9),
    ]
    pick, swapped = lookahead_pick(ranked, picks_until_next=3)
    assert pick.entry.name == "Star" and swapped is None


def test_lookahead_needs_a_known_next_pick_and_survival():
    ranked = [_rp("A", "WR", 100.0, 0.95), _rp("B", "RB", 95.0, 0.1)]
    assert lookahead_pick(ranked, picks_until_next=None)[0].entry.name == "A"
    ranked[0].survival = None
    assert lookahead_pick(ranked, picks_until_next=3)[0].entry.name == "A"


def test_recommendation_narrates_the_swap_and_survival():
    from fantasy_coach.clients.models import LeagueSettings, RosterPosition

    settings = LeagueSettings(
        league_key="x", max_teams=2,
        roster_positions=[RosterPosition(position="RB", count=1),
                          RosterPosition(position="WR", count=1)],
    )
    needs = compute_needs(roster_slots(settings))
    ranked = [
        _rp("Safe Star", "WR", 100.0, 0.95, adp=20.0),
        _rp("Hot Back", "RB", 92.0, 0.20, adp=12.0),
        _rp("Filler", "TE", 60.0, 0.9),
        _rp("Fallback", "QB", 50.0, 0.9),
    ]
    rec = build_recommendation(ranked, needs, current_pick=10, picks_until_next=3)
    assert rec.player.entry.name == "Hot Back"
    assert rec.swapped_from.entry.name == "Safe Star"
    assert any(r.startswith("Timing: take now") and "Safe Star" in r for r in rec.reasons)
    # Without the lookahead inputs the value pick stands and survival is narrated.
    plain = build_recommendation(ranked, needs, current_pick=10)
    assert plain.player.entry.name == "Safe Star" and plain.swapped_from is None
    assert any("Likely there next time too (95%)" in r for r in plain.reasons)
    # The plan line names the best safe runner-up (Filler, 90%), never the pick.
    assert any(r.startswith("Plan: Filler") and "(90%)" in r for r in plain.reasons)
    assert any(r.startswith("Plan: Safe Star") for r in rec.reasons)


# -- the live loop ------------------------------------------------------------


def test_loop_stamps_survival_and_updates_it_as_picks_land(draft_store, draft_settings):
    order = [MY_TEAM, OPP_TEAM]
    # Full 16-pick Yahoo shape, none made yet.
    blank = [make_pick(n, "", "") for n in range(1, 17)]
    for p in blank:
        p.player_key = ""
    src = ListSource(blank)
    loop, _ = make_loop(draft_store, draft_settings, src, draft_order=order)
    snap = loop.poll_once()
    # I'm on the clock at pick 1; my next is pick 4 (snake) → p_after is live.
    assert snap["draft"]["my_next_pick"] == 1 and snap["draft"]["my_pick_after"] == 4
    rec = snap["recommendation"]
    assert rec["survival"]["p_next"] == 1.0
    assert rec["survival"]["p_after"] is not None
    assert all(p["survival"] is not None for p in snap["available"])
    # R1 (ADP 1) is far less likely to survive two picks than W5 (ADP 8, no...
    # W4 ADP 9): monotone in ADP.
    by = {p["name"]: p["survival"]["p_after"] for p in snap["available"]}
    assert by["Rusher One"] < by["Wideout Four"]

    # Opponent takes R2 and R3 (a mini RB run) → my next pick 4 is on the clock;
    # the room state now shows recent positions and survival re-estimates.
    src.picks = [make_pick(1, MY_TEAM, "201"), make_pick(2, OPP_TEAM, "202"),
                 make_pick(3, OPP_TEAM, "203")] + blank[3:]
    snap2 = loop.poll_once()
    assert snap2["draft"]["current_pick"] == 4 and snap2["draft"]["my_next_pick"] == 4
    assert snap2["survival"]["recent_positions"] == ["RB", "RB", "RB"]
    assert snap2["draft"]["my_pick_after"] == 5


def test_survival_probabilities_are_monotone_in_next_pick_distance(draft_store, draft_settings):
    blank = [make_pick(n, "", "") for n in range(1, 17)]
    for p in blank:
        p.player_key = ""
    # Slot 2 in a 2-team snake: my picks are 2, 3, 6, 7 … → at pick 1 my next
    # is 2 (1 pick away) and the one after 3.
    loop, _ = make_loop(
        draft_store, draft_settings, ListSource(blank),
        my_team_key=OPP_TEAM, draft_order=[MY_TEAM, OPP_TEAM],
    )
    snap = loop.poll_once()
    assert snap["draft"]["my_next_pick"] == 2 and snap["draft"]["my_pick_after"] == 3
    for p in snap["available"]:
        sv = p["survival"]
        assert sv["p_after"] <= sv["p_next"] <= 1.0
