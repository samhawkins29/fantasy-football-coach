"""Tests for the VORP value board (M4 §4.2).

Every number is hand-computable. The load-bearing invariants:

* replacement baselines come from the league's *roster demand* (dedicated slots
  + greedy FLEX/superflex consumption), never from constants;
* VORP = league-scored points − baseline(position);
* scoring settings move the ranking in the expected direction (PPR lifts
  pass-catchers, superflex lifts QBs, more RB slots deepen the RB baseline);
* rookies / K / DEF without projections enter via labelled ADP/flat fallbacks
  instead of silently vanishing.
"""

from __future__ import annotations

import math

import pytest

from fantasy_coach.clients.models import LeagueSettings, RosterPosition, StatCategory
from fantasy_coach.ingest.canonical import CanonicalPlayer, ExternalIds
from fantasy_coach.ingest.sources import ProjectionRecord
from fantasy_coach.value.board import (
    SOURCE_ADP,
    SOURCE_FLAT,
    SOURCE_PROJECTION,
    assign_tiers,
    build_value_board,
    replacement_baselines,
    starter_demand,
)

#: Half-PPR, 4-pt pass TD — Yahoo-default-shaped modifiers by stat id.
HALF_PPR = {4: 0.04, 5: 4.0, 6: -1.0, 9: 0.1, 10: 6.0, 11: 0.5, 12: 0.1, 13: 6.0, 16: 2.0, 18: -2.0}


def make_settings(
    roster: list[tuple[str, int]],
    scoring: dict[int, float] = HALF_PPR,
    *,
    max_teams: int = 12,
) -> LeagueSettings:
    return LeagueSettings(
        max_teams=max_teams,
        roster_positions=[
            RosterPosition(
                position=pos,
                count=count,
                is_starting_position=pos not in ("BN", "IR"),
            )
            for pos, count in roster
        ],
        stat_categories=[StatCategory(stat_id=sid, value=val) for sid, val in scoring.items()],
    )


def proj(gsis: str, name: str, position: str, **stats: float) -> ProjectionRecord:
    return ProjectionRecord(
        source="test",
        source_id=gsis,
        source_id_field="gsis_id",
        points=0.0,
        position=position,
        name=name,
        stats=dict(stats),
    )


def canon(
    canonical_id: str,
    name: str,
    position: str,
    *,
    gsis: str | None = None,
    adp: float | None = None,
) -> CanonicalPlayer:
    player = CanonicalPlayer(
        canonical_id=canonical_id,
        ids=ExternalIds(gsis_id=gsis),
        name=name,
        position=position,
    )
    player.market.adp = adp
    return player


# -- starter demand + replacement baselines (hand-computable) ------------------


def test_starter_demand_scales_by_teams_and_splits_flex():
    settings = make_settings([("QB", 1), ("RB", 2), ("WR", 3), ("W/R/T", 1), ("BN", 5)])
    dedicated, flex = starter_demand(settings, num_teams=10)
    assert dedicated == {"QB": 10, "RB": 20, "WR": 30}
    assert flex == [(("WR", "RB", "TE"), 10)]


def test_starter_demand_never_counts_bench_even_if_flagged_starting():
    settings = LeagueSettings(
        roster_positions=[
            RosterPosition(position="RB", count=2),
            RosterPosition(position="BN", count=5, is_starting_position=True),  # bad parse
            RosterPosition(position="IR", count=2, is_starting_position=True),
        ]
    )
    dedicated, flex = starter_demand(settings, num_teams=4)
    assert dedicated == {"RB": 8}
    assert flex == []


def test_superflex_slot_includes_qb_in_eligible_positions():
    settings = make_settings([("QB", 1), ("Q/W/R/T", 1)])
    _, flex = starter_demand(settings, num_teams=2)
    assert flex == [(("QB", "WR", "RB", "TE"), 2)]
    assert settings.is_superflex


def test_baselines_dedicated_plus_greedy_flex():
    # 2 teams, QB1 / RB2 / WR1 / W-R flex 1.
    # Dedicated demand: QB 2, RB 4, WR 2. Flex (2 slots) greedily takes the best
    # remaining: WR 175 (beats RB 160), then WR 165 (beats RB 160).
    # -> Baselines: QB idx2 = 260; RB idx4 = 160; WR exhausted -> last = 165.
    settings = make_settings([("QB", 1), ("RB", 2), ("WR", 1), ("W/R", 1)])
    pools = {
        "QB": [300.0, 280.0, 260.0],
        "RB": [200.0, 190.0, 180.0, 170.0, 160.0, 150.0],
        "WR": [195.0, 185.0, 175.0, 165.0],
    }
    baselines = replacement_baselines(pools, settings, num_teams=2)
    assert baselines == {"QB": 260.0, "RB": 160.0, "WR": 165.0}


def test_more_rb_slots_push_the_rb_baseline_deeper():
    pools = {"RB": [100.0, 90.0, 80.0, 70.0, 60.0, 50.0, 40.0]}
    two_rb = make_settings([("RB", 2)])
    three_rb = make_settings([("RB", 3)])
    assert replacement_baselines(pools, two_rb, num_teams=2)["RB"] == 60.0
    assert replacement_baselines(pools, three_rb, num_teams=2)["RB"] == 40.0


def test_superflex_drains_the_qb_pool():
    # QBs outscore everyone, so a Q/W/R/T flex consumes QBs: baseline drops from
    # QB3 (380) in the 1QB league to QB5 (360) in superflex.
    pools = {
        "QB": [400.0, 390.0, 380.0, 370.0, 360.0],
        "RB": [200.0, 190.0, 180.0],
        "WR": [195.0, 185.0, 175.0],
        "TE": [150.0, 140.0, 130.0],
    }
    one_qb = make_settings([("QB", 1), ("W/R/T", 1)])
    superflex = make_settings([("QB", 1), ("Q/W/R/T", 1)])
    assert replacement_baselines(pools, one_qb, num_teams=2)["QB"] == 380.0
    assert replacement_baselines(pools, superflex, num_teams=2)["QB"] == 360.0


def test_flex_takes_best_available_across_positions():
    # One flex slot, 1 team, no dedicated slots: it should consume the single
    # best player across WR/RB/TE (the RB at 90), moving only RB's baseline.
    pools = {"RB": [90.0, 50.0], "WR": [80.0, 40.0], "TE": [70.0, 30.0]}
    settings = make_settings([("W/R/T", 1)])
    baselines = replacement_baselines(pools, settings, num_teams=1)
    assert baselines == {"RB": 50.0, "WR": 80.0, "TE": 70.0}


# -- tiers ---------------------------------------------------------------------


def test_assign_tiers_breaks_on_fixed_gap():
    assert assign_tiers([100.0, 98.0, 90.0, 88.0, 70.0], fixed_gap=5.0) == [1, 1, 2, 2, 3]


def test_assign_tiers_adaptive_keeps_flat_positions_together():
    # Gaps of 1 each: threshold = max(min_gap=10, 1.5*mean=1.5) = 10 -> one tier.
    assert assign_tiers([100.0, 99.0, 98.0]) == [1, 1, 1]


def test_assign_tiers_empty_and_singleton():
    assert assign_tiers([]) == []
    assert assign_tiers([50.0]) == [1]


# -- the board: VORP + ranking direction ---------------------------------------


def two_team_wr_league() -> LeagueSettings:
    return make_settings([("WR", 1)], max_teams=2)


def test_vorp_is_points_minus_positional_baseline():
    # 2-team WR1 league: baseline = WR3's points. Points (half-PPR):
    # A: 1000*0.1 + 40*0.5 = 120 ; B: 800*0.1 = 80 ; C: 500*0.1 = 50.
    board = build_value_board(
        [
            proj("A", "Alpha", "WR", rec_yds=1000.0, rec=40.0),
            proj("B", "Beta", "WR", rec_yds=800.0),
            proj("C", "Gamma", "WR", rec_yds=500.0),
        ],
        two_team_wr_league(),
    )
    assert board.baselines == {"WR": 50.0}
    by_name = {e.name: e for e in board.entries}
    assert by_name["Alpha"].points == pytest.approx(120.0)
    assert by_name["Alpha"].vorp == pytest.approx(70.0)
    assert by_name["Beta"].vorp == pytest.approx(30.0)
    assert by_name["Gamma"].vorp == pytest.approx(0.0)
    assert [e.overall_rank for e in board.entries] == [1, 2, 3]
    assert [e.pos_rank for e in board.entries] == [1, 2, 3]
    assert all(e.value_source == SOURCE_PROJECTION for e in board.entries)


def catcher_vs_rusher_projections() -> list[ProjectionRecord]:
    """A reception-heavy WR vs a TD-heavy RB, plus zero-catch filler for baselines."""
    return [
        proj("WR1", "Target Hog", "WR", rec=100.0, rec_yds=1100.0, rec_td=6.0),
        proj("RB1", "Volume Back", "RB", rush_yds=1400.0, rush_td=11.0),
        proj("WR2", "Depth Wr", "WR", rec_yds=500.0),
        proj("RB2", "Depth Rb", "RB", rush_yds=500.0),
    ]


def test_ppr_lifts_pass_catchers_over_standard():
    # Standard: WR1 = 110+36 = 146 < RB1 = 140+66 = 206.
    # Full PPR: WR1 = 246 > RB1 = 206. Baselines (50) identical either way.
    roster = [("RB", 1), ("WR", 1)]
    standard = build_value_board(
        catcher_vs_rusher_projections(),
        make_settings(roster, HALF_PPR | {11: 0.0}, max_teams=1),
    )
    ppr = build_value_board(
        catcher_vs_rusher_projections(),
        make_settings(roster, HALF_PPR | {11: 1.0}, max_teams=1),
    )
    std_names = [e.name for e in standard.top(2)]
    ppr_names = [e.name for e in ppr.top(2)]
    assert std_names == ["Volume Back", "Target Hog"]
    assert ppr_names == ["Target Hog", "Volume Back"]


def qb_heavy_projections() -> list[ProjectionRecord]:
    """QBs outscore skill players in raw points (as in real life)."""
    return [
        proj("Q1", "Gunslinger", "QB", pass_yds=4800.0, pass_td=38.0),
        proj("Q2", "Game Manager", "QB", pass_yds=4000.0, pass_td=28.0),
        proj("Q3", "Backup Qb", "QB", pass_yds=3200.0, pass_td=20.0),
        proj("Q4", "Clipboard Qb", "QB", pass_yds=2500.0, pass_td=12.0),
        proj("R1", "Bell Cow", "RB", rush_yds=1500.0, rush_td=12.0),
        proj("R2", "Committee Back", "RB", rush_yds=1000.0, rush_td=6.0),
        proj("R3", "Handcuff", "RB", rush_yds=500.0, rush_td=2.0),
        proj("W1", "Alpha Wr", "WR", rec=90.0, rec_yds=1300.0, rec_td=9.0),
        proj("W2", "Solid Wr", "WR", rec=70.0, rec_yds=900.0, rec_td=5.0),
        proj("W3", "Depth Wr", "WR", rec=40.0, rec_yds=500.0, rec_td=2.0),
    ]


def test_superflex_lifts_qbs_up_the_board():
    one_qb = build_value_board(
        qb_heavy_projections(),
        make_settings([("QB", 1), ("RB", 1), ("WR", 1), ("W/R", 1)], max_teams=2),
    )
    superflex = build_value_board(
        qb_heavy_projections(),
        make_settings([("QB", 1), ("RB", 1), ("WR", 1), ("Q/W/R", 1)], max_teams=2),
    )

    def rank(board, name):
        return next(e.overall_rank for e in board.entries if e.name == name)

    # The superflex slot drains the QB pool -> deeper QB baseline -> more QB VORP.
    assert superflex.baselines["QB"] < one_qb.baselines["QB"]
    assert rank(superflex, "Gunslinger") < rank(one_qb, "Gunslinger")
    assert rank(superflex, "Game Manager") < rank(one_qb, "Game Manager")


def test_num_teams_defaults_to_settings_and_moves_baselines():
    pools = [
        proj(f"R{i}", f"Rb {i}", "RB", rush_yds=1000.0 - 100.0 * i) for i in range(6)
    ]
    small = build_value_board(pools, make_settings([("RB", 1)], max_teams=2))
    large = build_value_board(pools, make_settings([("RB", 1)], max_teams=4))
    assert small.num_teams == 2
    assert large.num_teams == 4
    assert large.baselines["RB"] < small.baselines["RB"]
    override = build_value_board(pools, make_settings([("RB", 1)], max_teams=2), num_teams=4)
    assert override.baselines["RB"] == large.baselines["RB"]


# -- gap-fill: rookies via ADP, K/DEF via ADP or flat --------------------------


def test_rookie_without_projection_gets_adp_derived_value():
    # 1-team WR1 league. Points: A=200, B=100, C=60 -> baseline = B = 100.
    # VORP: A=100, B=0. ADP pairs (1, 100) and (e, 0) fit vorp = 100 - 100·ln(adp).
    # Rookie at adp e^0.5 -> pseudo-VORP 50, labelled "adp", slotted between A and B.
    projections = [
        proj("A", "Vet Alpha", "WR", rec_yds=2000.0),
        proj("B", "Vet Beta", "WR", rec_yds=1000.0),
        proj("C", "Vet Gamma", "WR", rec_yds=600.0),
    ]
    players = [
        canon("A", "Vet Alpha", "WR", gsis="A", adp=1.0),
        canon("B", "Vet Beta", "WR", gsis="B", adp=math.e),
        canon("UNK_9001", "Hot Rookie", "WR", adp=math.exp(0.5)),
    ]
    board = build_value_board(projections, make_settings([("WR", 1)], max_teams=1), players=players)
    rookie = next(e for e in board.entries if e.name == "Hot Rookie")
    assert rookie.value_source == SOURCE_ADP
    assert rookie.points is None
    assert rookie.vorp == pytest.approx(50.0)
    assert [e.name for e in board.entries[:3]] == ["Vet Alpha", "Hot Rookie", "Vet Beta"]
    assert rookie.pos_rank == 2  # participates in the positional view too


def test_kicker_and_defense_never_vanish():
    projections = [
        proj("A", "Vet Alpha", "WR", rec_yds=2000.0),
        proj("B", "Vet Beta", "WR", rec_yds=1000.0),
    ]
    players = [
        canon("A", "Vet Alpha", "WR", gsis="A", adp=1.0),
        canon("B", "Vet Beta", "WR", gsis="B", adp=10.0),
        canon("K1", "Leg Guy", "K", adp=140.0),  # ADP-known kicker
        canon("K2", "Other Leg", "K"),  # no ADP at all
        CanonicalPlayer.for_defense("SF"),  # no ADP defense
    ]
    board = build_value_board(
        projections, make_settings([("WR", 1), ("K", 1), ("DEF", 1)], max_teams=1), players=players
    )
    by_name = {e.name: e for e in board.entries}
    assert by_name["Leg Guy"].value_source == SOURCE_ADP
    assert by_name["Other Leg"].value_source == SOURCE_FLAT
    # A kicker no mock room even drafts must sit BELOW the market-priced one —
    # a flat 0.0 would absurdly outrank an ADP whose curve value is negative.
    assert by_name["Other Leg"].vorp < by_name["Leg Guy"].vorp
    assert by_name["SF DST"].value_source == SOURCE_FLAT
    assert by_name["SF DST"].position == "DEF"
    assert by_name["SF DST"].vorp == 0.0  # no priced DEF peers — plain flat
    assert board.skipped_no_signal == 0


def test_unstartable_positions_are_dropped_and_counted():
    # No-kicker league (like the founder's): a K with ADP is still worthless.
    projections = [
        proj("A", "Vet Alpha", "WR", rec_yds=2000.0),
        proj("B", "Vet Beta", "WR", rec_yds=1000.0),
        proj("K9", "Proj Kicker", "K", rec_yds=0.0),
    ]
    players = [
        canon("A", "Vet Alpha", "WR", gsis="A", adp=1.0),
        canon("K1", "Leg Guy", "K", adp=140.0),
    ]
    board = build_value_board(projections, make_settings([("WR", 1)], max_teams=1), players=players)
    assert {e.position for e in board.entries} == {"WR"}
    assert board.skipped_unstartable == 2  # the projected K and the ADP K
    # Settings without any roster definition drop nothing.
    from fantasy_coach.clients.models import LeagueSettings

    open_board = build_value_board(projections, LeagueSettings(max_teams=1), players=players)
    assert {e.position for e in open_board.entries} == {"WR", "K"}


def test_skill_player_with_no_projection_and_no_adp_is_counted_not_listed():
    projections = [proj("A", "Vet Alpha", "WR", rec_yds=1000.0)]
    players = [
        canon("A", "Vet Alpha", "WR", gsis="A"),
        canon("UNK_1", "Camp Body", "WR"),  # no projection, no ADP, not K/DEF
    ]
    board = build_value_board(projections, make_settings([("WR", 1)], max_teams=1), players=players)
    assert board.skipped_no_signal == 1
    assert all(e.name != "Camp Body" for e in board.entries)


def test_adp_curve_with_too_few_pairs_falls_back_flat():
    # Only one projected player has an ADP -> curve degenerates to that VORP.
    projections = [
        proj("A", "Vet Alpha", "WR", rec_yds=1500.0),
        proj("B", "Vet Beta", "WR", rec_yds=1000.0),
    ]
    players = [
        canon("A", "Vet Alpha", "WR", gsis="A", adp=5.0),
        canon("UNK_2", "Adp Rookie", "WR", adp=30.0),
    ]
    board = build_value_board(projections, make_settings([("WR", 1)], max_teams=1), players=players)
    vet_alpha = next(e for e in board.entries if e.name == "Vet Alpha")
    rookie = next(e for e in board.entries if e.name == "Adp Rookie")
    assert rookie.vorp == pytest.approx(vet_alpha.vorp)


# -- canonical write-back + board views ----------------------------------------


def test_board_writes_value_back_onto_canonical_players():
    projections = [
        proj("A", "Vet Alpha", "WR", rec_yds=1000.0),
        proj("B", "Vet Beta", "WR", rec_yds=600.0),
    ]
    alpha = canon("A", "Vet Alpha", "WR", gsis="A")
    kicker = canon("K1", "Leg Guy", "K")
    board = build_value_board(
        projections, make_settings([("WR", 1), ("K", 1)], max_teams=1), players=[alpha, kicker]
    )
    entry = next(e for e in board.entries if e.name == "Vet Alpha")
    assert alpha.value.vorp == entry.vorp
    assert alpha.value.tier == entry.tier
    assert alpha.value.scoring_adjusted_points == entry.points
    assert kicker.value.vorp == 0.0  # flat entries write back too


def test_at_position_and_top_views():
    board = build_value_board(
        qb_heavy_projections(),
        make_settings([("QB", 1), ("RB", 1), ("WR", 1)], max_teams=2),
    )
    assert len(board.top(4)) == 4
    rbs = board.at_position("RB")
    assert [e.name for e in rbs] == ["Bell Cow", "Committee Back", "Handcuff"]
    assert [e.pos_rank for e in rbs] == [1, 2, 3]


def test_projection_without_stats_uses_prescored_points():
    # A consensus source that only has a total: it still boards (labelled
    # projection-based) using its own points, since there's nothing to rescore.
    rec = ProjectionRecord(
        source="consensus", source_id="X", source_id_field="gsis_id", points=222.0, position="WR", name="Prescored"
    )
    board = build_value_board([rec], make_settings([("WR", 1)], max_teams=1))
    assert board.entries[0].points == pytest.approx(222.0)


# --------------------------------------------------------------------------- #
# Streaming positions (P0-3): raised baselines + compressed VORP
# --------------------------------------------------------------------------- #


def _flat_rec(gid, pos, points):
    return ProjectionRecord(
        source="t", source_id=gid, source_id_field="gsis_id",
        points=points, position=pos, name=gid, stats={},
    )


def _idp_settings(num_teams=10):
    # Sam's shape: 1 QB, 2 RB, 1 IDP flex ("D"), 1 DEF.
    return make_settings(
        [("QB", 1), ("RB", 2), ("D", 1), ("BN", 3)], max_teams=num_teams
    )


def test_streaming_baseline_raises_to_band_mean_and_compresses_vorp():
    from fantasy_coach.value.board import STREAM_VALUE_FACTOR

    # 30 LBs at 200, 199, …, 171: demand (10 D-flex slots, all LB) baseline
    # = LB11 = 190; the streaming band (ranks 10..25, num_teams=10) mean =
    # mean(191..176) = 183.5 < 190, so the demand baseline stands and the
    # residual is compressed: LB1 vorp = 0.5 × (200 − 190) = 5.
    recs = [_flat_rec(f"LB{i}", "LB", 200.0 - (i - 1)) for i in range(1, 31)]
    recs += [_flat_rec(f"QB{i}", "QB", 300.0 - 10 * (i - 1)) for i in range(1, 15)]
    recs += [_flat_rec(f"RB{i}", "RB", 250.0 - 5 * (i - 1)) for i in range(1, 31)]
    board = build_value_board(recs, _idp_settings(), num_teams=10)
    lb1 = next(e for e in board.entries if e.canonical_id == "LB1")
    raw = 200.0 - board.baselines["LB"]
    assert lb1.vorp == pytest.approx(raw * STREAM_VALUE_FACTOR)
    assert lb1.vorp < raw  # compressed, never inflated


def test_streaming_band_raises_a_too_generous_demand_baseline():
    # Steep LB pool where demand would set a low baseline: only ~4 slots'
    # worth of demand (num_teams=4) but 30 LBs — the streaming band mean
    # (ranks 4..10) exceeds the demand baseline and wins (max of the two).
    from fantasy_coach.value.board import replacement_baselines, streaming_baselines

    pool = [200.0, 150.0, 120.0, 100.0, 95.0, 90.0, 85.0, 80.0, 75.0, 70.0, 65.0]
    settings = _idp_settings(num_teams=4)
    base = replacement_baselines({"LB": pool}, settings, 4)
    raised = streaming_baselines(base, {"LB": pool}, 4)
    assert raised["LB"] >= base["LB"]
    # Non-streaming positions pass through untouched.
    base_rb = replacement_baselines({"RB": pool}, settings, 4)
    assert streaming_baselines(base_rb, {"RB": pool}, 4)["RB"] == base_rb["RB"]


def test_streaming_entries_skip_the_playoff_blend():
    # A juicy playoff schedule must not reinflate a compressed IDP value —
    # streamed positions pick their own matchups weekly.
    from fantasy_coach.ingest.schedule import SeasonSchedule

    opponents = {"KC": {w: "OPP" for w in range(1, 18) if w != 9}}
    sched = SeasonSchedule(
        season=2026, opponents=opponents, byes={"KC": 9},
        multipliers={"LB": {"OPP": 1.25}, "RB": {"OPP": 1.25}},
    )
    recs = [_flat_rec(f"LB{i}", "LB", 200.0 - i) for i in range(1, 31)]
    recs += [_flat_rec(f"RB{i}", "RB", 250.0 - 5 * i) for i in range(1, 31)]
    for r in recs:
        r.team = "KC"
    settings = LeagueSettings(
        max_teams=10,
        playoff_start_week=15,
        num_playoff_teams=6,
        uses_playoff=True,
        roster_positions=[
            RosterPosition(position="RB", count=2),
            RosterPosition(position="D", count=1),
        ],
    )
    board = build_value_board(recs, settings, num_teams=10, schedule=sched, playoff_weight=0.5)
    lb1 = next(e for e in board.entries if e.canonical_id == "LB1")
    rb1 = next(e for e in board.entries if e.canonical_id == "RB1")
    assert lb1.draft_value is None and lb1.playoff_vorp is None  # no blend
    assert rb1.draft_value is not None  # normal positions still blend
