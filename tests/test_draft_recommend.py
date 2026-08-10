"""Roster-need weighting + the recommendation (M5 §4.2–4.3)."""

from __future__ import annotations

import pytest

from fantasy_coach.draft.recommend import (
    NEED_DEPTH,
    NEED_FLEX,
    NEED_STARTER,
    NEED_WEIGHTS,
    assign_roster,
    build_recommendation,
    compute_needs,
    rank_available,
    roster_slots,
)
from fantasy_coach.value.board import BoardEntry, ValueBoard


@pytest.fixture
def slots(draft_settings):
    return roster_slots(draft_settings)


def entry(cid, name, pos, vorp, *, tier=1, pos_rank=1, rank=1, adp=None, source="projection"):
    return BoardEntry(
        canonical_id=cid,
        name=name,
        position=pos,
        vorp=vorp,
        points=vorp + 100,
        value_source=source,
        overall_rank=rank,
        pos_rank=pos_rank,
        tier=tier,
        adp=adp,
    )


def board(*entries):
    return ValueBoard(entries=list(entries))


# -- roster slots -------------------------------------------------------------


def test_roster_slots_expand_counts_and_skip_ir(slots):
    assert [s.label for s in slots] == [
        "QB", "RB", "RB", "WR", "WR", "TE", "W/R/T", "BN",
    ]
    flex = slots[6]
    assert flex.is_flex and set(flex.eligible) == {"WR", "RB", "TE"}
    assert slots[7].is_bench


def test_assign_roster_dedicated_then_flex_then_bench(slots):
    rbs = [{"name": f"RB {i}", "position": "RB"} for i in range(4)]
    assign_roster(slots, rbs)
    filled = [s.label for s in slots if s.player is not None]
    assert filled == ["RB", "RB", "W/R/T", "BN"]


def test_assign_roster_unknown_position_goes_to_bench(slots):
    assign_roster(slots, [{"name": "Unmapped pick", "position": ""}])
    assert slots[7].player is not None  # the bench slot
    assert all(s.player is None for s in slots[:7])


def test_assign_roster_overflows_extra_bench_rather_than_dropping(slots):
    players = [{"name": f"P{i}", "position": "QB"} for i in range(4)]
    assign_roster(slots, players)
    # 1 QB slot + 1 flex? QB not flex-eligible in W/R/T -> QB, BN, then overflow
    assert sum(1 for s in slots if s.player is not None) == 4
    assert len(slots) == 10  # two overflow bench rows appended


# -- needs + weights ----------------------------------------------------------


def test_needs_open_starter_flex_depth_progression(slots):
    needs = compute_needs(slots)
    assert needs.tag("RB") == NEED_STARTER
    assert needs.weight("RB") == 1.0

    assign_roster(slots, [{"position": "RB"}, {"position": "RB"}])
    needs = compute_needs(slots)
    assert needs.tag("RB") == NEED_FLEX          # dedicated full, flex open
    assert needs.weight("RB") == NEED_WEIGHTS[NEED_FLEX]

    assign_roster(slots, [{"position": "RB"}])   # third RB takes the flex
    needs = compute_needs(slots)
    assert needs.tag("RB") == NEED_DEPTH
    assert needs.weight("RB") == NEED_WEIGHTS[NEED_DEPTH]
    assert needs.tag("WR") == NEED_STARTER       # unaffected position


# -- ranking ------------------------------------------------------------------


def test_rank_weighting_prefers_open_slot_over_filled_position(slots):
    # RB starters + flex all filled: a 40-VORP RB (depth .55 -> 22) must fall
    # behind a 30-VORP WR filling an open starter slot (30).
    assign_roster(slots, [{"position": "RB"}] * 3)
    needs = compute_needs(slots)
    ranked = rank_available(
        board(
            entry("RBX", "Depth Rb", "RB", 40.0, rank=1),
            entry("WRX", "Starter Wr", "WR", 30.0, rank=2),
        ),
        needs,
    )
    assert [rp.entry.canonical_id for rp in ranked] == ["WRX", "RBX"]
    assert ranked[0].score == 30.0
    assert ranked[1].score == pytest.approx(22.0)
    assert ranked[1].need == NEED_DEPTH


def test_negative_vorp_is_never_inflated_by_downweighting(slots):
    assign_roster(slots, [{"position": "RB"}] * 3)
    needs = compute_needs(slots)
    ranked = rank_available(board(entry("RBX", "Bad Rb", "RB", -10.0)), needs)
    assert ranked[0].score == -10.0  # not -10 * 0.55


def test_cliff_flags_last_player_of_tier(slots):
    needs = compute_needs(slots)
    ranked = rank_available(
        board(
            entry("T1", "Te One", "TE", 70.0, tier=1, pos_rank=1, rank=1),
            entry("T2", "Te Two", "TE", 10.0, tier=2, pos_rank=2, rank=2),
            entry("T3", "Te Three", "TE", 8.0, tier=2, pos_rank=3, rank=3),
        ),
        needs,
    )
    by_id = {rp.entry.canonical_id: rp for rp in ranked}
    assert by_id["T1"].cliff and by_id["T1"].cliff_drop == 60.0
    assert not by_id["T2"].cliff  # mid-tier
    assert not by_id["T3"].cliff  # last at position, no next player


# -- recommendation -----------------------------------------------------------


def test_recommendation_narrates_value_need_cliff_and_adp(slots):
    needs = compute_needs(slots)
    ranked = rank_available(
        board(
            entry("T1", "Te One", "TE", 70.0, tier=1, pos_rank=1, rank=1, adp=5.0),
            entry("T2", "Te Two", "TE", 10.0, tier=2, pos_rank=2, rank=2),
        ),
        needs,
    )
    rec = build_recommendation(ranked, needs, current_pick=12)
    assert rec is not None and rec.player.entry.canonical_id == "T1"
    text = " ".join(rec.reasons)
    assert "VORP +70.0" in text
    assert "open TE starting slot" in text
    assert "60 pt cliff" in text  # last of tier 1, 60-pt drop
    assert "ADP 5" in text  # falling: pick 12 vs ADP 5


def test_recommendation_flags_non_projection_value_source(slots):
    needs = compute_needs(slots)
    ranked = rank_available(
        board(entry("K1", "Rookie Rb", "RB", 12.0, source="adp")), needs
    )
    rec = build_recommendation(ranked, needs)
    assert any("adp-derived" in r for r in rec.reasons)


def test_recommendation_none_on_empty_board(slots):
    assert build_recommendation([], compute_needs(slots)) is None
