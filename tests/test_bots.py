"""Realistic mock-draft opponents (upgrade 4).

Behavioural assertions, all deterministic (seeded RNG, small pick numbers so
the noise term can't flip a hand-built comparison):

* the scripted draft is reproducible per seed and differs across seeds;
* every bot ends with a fillable starting lineup and respects the hard
  caps; K/DEF go only in the last rounds and *do* get drafted;
* need: a bot with an open starting slot takes the starter over a bench
  body of equal market value;
* runs: a run-sensitive bot who still needs the position joins a run; one
  whose starters are set does not;
* handcuff: a handcuffer with an elite RB takes that RB's backup late over
  an equal-market RB elsewhere;
* bye: a bye-aware bot avoids a third starter on the same bye among
  near-equals;
* reach: reachers stray further from market order than market bots;
* archetype bias: the QB-early bot takes the elite QB earlier.
"""

from __future__ import annotations

from collections import Counter

import pytest

from fantasy_coach.clients.models import LeagueSettings, RosterPosition
from fantasy_coach.draft.bots import (
    ARCHETYPES,
    KICKER_ROUNDS_LEFT,
    BotProfile,
    BotRoom,
    profiles_for_room,
)
from fantasy_coach.draft.recommend import assign_roster, compute_needs, roster_slots
from fantasy_coach.draft.simulate import script_draft


# -- a hand-built board -------------------------------------------------------


def _settings(num_teams=2, *, kicker=False):
    roster = [
        RosterPosition(position="QB", count=1),
        RosterPosition(position="RB", count=2),
        RosterPosition(position="WR", count=2),
        RosterPosition(position="TE", count=1),
        RosterPosition(position="W/R/T", count=1),
    ]
    if kicker:
        roster += [RosterPosition(position="K", count=1), RosterPosition(position="DEF", count=1)]
    roster += [RosterPosition(position="BN", count=3, is_starting_position=False)]
    return LeagueSettings(league_key="bots.l.1", max_teams=num_teams, roster_positions=roster)


def _row(cid, pos, adp, rank, *, team="KC", pos_rank=None, bye=None, name=None):
    return {
        "canonical_id": cid, "name": name or cid, "position": pos, "team": team,
        "adp": adp, "overall_rank": rank, "pos_rank": pos_rank or 1, "bye_week": bye,
    }


def _board(n_per_pos=12, *, kicker=False):
    """A market-ordered board: positions interleaved, ADP == overall rank."""
    rows = []
    rank = 0
    for i in range(1, n_per_pos + 1):
        for pos in ("RB", "WR", "QB", "TE"):
            rank += 1
            rows.append(_row(f"{pos}{i}", pos, float(rank), rank, team=f"T{i}", pos_rank=i, bye=(i % 4) + 5))
    if kicker:
        for i in range(1, 6):
            rank += 1
            rows.append(_row(f"K{i}", "K", float(rank), rank, pos_rank=i))
            rank += 1
            rows.append(_row(f"DEF{i}", "DEF", float(rank), rank, pos_rank=i))
    return rows


def _snake(num_teams, rounds):
    order = [f"t{i}" for i in range(1, num_teams + 1)]
    for pick_no in range(1, rounds * num_teams + 1):
        rnd, idx = divmod(pick_no - 1, num_teams)
        team = order[idx] if rnd % 2 == 0 else order[num_teams - 1 - idx]
        yield pick_no, rnd + 1, team


def _run_room(profiles, *, num_teams=2, rounds=8, kicker=False, seed=0, board=None):
    settings = _settings(num_teams, kicker=kicker)
    room = BotRoom(board or _board(kicker=kicker), settings, [f"t{i}" for i in range(1, num_teams + 1)],
                   rounds=rounds, seed=seed, profiles=profiles)
    picks = []
    for pick_no, rnd, team in _snake(num_teams, rounds):
        picks.append((pick_no, rnd, team, room.pick(team, pick_no, rnd)))
    return room, picks, settings


# -- script-level ---------------------------------------------------------------


def test_script_is_reproducible_per_seed_and_varies_across_seeds(draft_store, draft_settings):
    a = [p.player_id for p in script_draft(draft_store, draft_settings, seed=1)]
    b = [p.player_id for p in script_draft(draft_store, draft_settings, seed=1)]
    c = [p.player_id for p in script_draft(draft_store, draft_settings, seed=2)]
    assert a == b
    assert sorted(a) == sorted(c)  # same pool fully drafted…
    assert a != c  # …in a different (equally plausible) order


def test_profiles_cycle_the_archetypes():
    profs = profiles_for_room(12)
    assert len(profs) == 12
    assert profs[0] is ARCHETYPES[0] and profs[10] is ARCHETYPES[0]
    assert {p.name for p in profs} == {p.name for p in ARCHETYPES}


# -- hard rules -----------------------------------------------------------------


def test_every_bot_fills_its_starters_and_respects_caps():
    room, picks, settings = _run_room(None, num_teams=4, rounds=10, kicker=True, seed=5)
    for team in ("t1", "t2", "t3", "t4"):
        roster = room.roster_of(team)
        needs = compute_needs(assign_roster(roster_slots(settings), roster))
        assert not needs.open_starters and not needs.open_flex, (team, needs)
        counts = Counter(r["position"] for r in roster)
        assert counts["QB"] <= 2 and counts["TE"] <= 2
        assert counts["K"] == 1 and counts["DEF"] == 1
    # K/DEF only in the last KICKER_ROUNDS_LEFT rounds.
    for pick_no, rnd, team, row in picks:
        if row["position"] in ("K", "DEF"):
            assert 10 - rnd + 1 <= KICKER_ROUNDS_LEFT


def test_no_backup_qb_or_te_mid_draft():
    _, picks, _ = _run_room(None, num_teams=4, rounds=10, seed=2)
    seen = {}
    for pick_no, rnd, team, row in picks:
        pos = row["position"]
        if pos in ("QB", "TE"):
            seen.setdefault((team, pos), []).append(rnd)
    for (team, pos), rounds in seen.items():
        if len(rounds) > 1:
            assert 10 - rounds[1] + 1 <= 5, (team, pos, rounds)


# -- behaviours -----------------------------------------------------------------


def _one_pick(profile, board, *, prior=(), pick_no=3, round_no=2, rounds=8, seed=0, external=()):
    """One bot pick after ``prior`` picks by the same team and ``external`` picks by others."""
    settings = _settings(2)
    room = BotRoom(board, settings, ["t1", "t2"], rounds=rounds, seed=seed, profiles=[profile, profile])
    n = 0
    for cid in external:
        n += 1
        room.record_external("t2", n, cid)
    for cid in prior:
        n += 1
        room.record_external("t1", n, cid)
    return room.pick("t1", pick_no, round_no)


def test_need_filler_takes_the_open_starter_over_equal_market_depth():
    # t1 already has two RBs; RB3 and WR1 sit at equal market value.
    board = [
        _row("RBa", "RB", 1.0, 1), _row("RBb", "RB", 2.0, 2),
        _row("RB3", "RB", 3.0, 3), _row("WR1", "WR", 3.0, 4),
        _row("QB1", "QB", 10.0, 10), _row("TE1", "TE", 11.0, 11),
    ]
    needy = BotProfile("needy", reach=0.0, value_weight=0.0, need_weight=1.5)
    pick = _one_pick(needy, board, prior=("RBa", "RBb"))
    assert pick["canonical_id"] == "WR1"


def test_run_pulls_in_the_bot_who_still_needs_the_position():
    # Eight of the last picks were RBs; the market expected a mix. Two bots
    # look at RB9 vs WR1 at equal market value.
    board = [_row(f"RB{i}", "RB", float(i), i) for i in range(1, 9)]
    board += [_row("RB9", "RB", 9.0, 9), _row("WR1", "WR", 9.0, 10)]
    board += [_row(f"WR{i}", "WR", 10.0 + i, 10 + i) for i in range(2, 8)]
    board += [_row("QB1", "QB", 30.0, 30), _row("TE1", "TE", 31.0, 31)]
    run = tuple(f"RB{i}" for i in range(1, 9))
    panic = BotProfile("panic", reach=0.0, value_weight=0.0, need_weight=0.0, run_sensitivity=1.4)
    calm = BotProfile("calm", reach=0.0, value_weight=0.0, need_weight=0.0, run_sensitivity=0.0)
    assert _one_pick(panic, board, external=run, pick_no=9)["canonical_id"] == "RB9"
    # Same panic bot with its RB starters already set ignores the run — the
    # tie then falls to whichever came first in market order (RB9), so use a
    # slightly better WR to make the point unambiguous.
    board2 = [dict(r) for r in board]
    for r in board2:
        if r["canonical_id"] == "WR1":
            r["adp"], r["overall_rank"] = 8.5, 9
        if r["canonical_id"] == "RB9":
            r["overall_rank"] = 10
    assert _one_pick(calm, board2, external=run, pick_no=9)["canonical_id"] == "WR1"
    settled = _one_pick(panic, board2, external=run[:5], prior=("RB6", "RB7", "RB8"), pick_no=9)
    assert settled["canonical_id"] == "WR1"


def test_handcuffer_takes_the_backup_of_his_own_rb1_late():
    board = [
        _row("RB1", "RB", 1.0, 1, team="SF", pos_rank=1),
        _row("QB1", "QB", 3.0, 3), _row("WR1", "WR", 4.0, 4), _row("WR2", "WR", 5.0, 5),
        _row("TE1", "TE", 6.0, 6),
        _row("RBx", "RB", 36.0, 36, team="DAL", pos_rank=26),  # best RB by market…
        _row("CUFF", "RB", 40.0, 40, team="SF", pos_rank=30),  # …vs my RB1's backup
        _row("OTHER", "RB", 40.0, 41, team="NYJ", pos_rank=31),
    ]
    cuffer = BotProfile("cuffer", reach=0.0, value_weight=0.0, need_weight=0.0, handcuff=1.0)
    plain = BotProfile("plain", reach=0.0, value_weight=0.0, need_weight=0.0, handcuff=0.0)
    prior = ("RB1", "QB1", "WR1", "WR2", "TE1")
    pick = _one_pick(cuffer, board, prior=prior, pick_no=12, round_no=6, rounds=8)
    assert pick["canonical_id"] == "CUFF"
    pick2 = _one_pick(plain, board, prior=prior, pick_no=12, round_no=6, rounds=8)
    assert pick2["canonical_id"] in ("RBx", "CUFF")  # market order, no handcuff pull
    assert pick2["canonical_id"] == "RBx"


def test_bye_aware_bot_avoids_a_third_shared_bye():
    board = [
        _row("A", "WR", 1.0, 1, bye=7), _row("B", "RB", 2.0, 2, bye=7),
        _row("C", "RB", 3.0, 3, bye=7), _row("D", "RB", 3.0, 4, bye=9),
        _row("QB1", "QB", 20.0, 20), _row("TE1", "TE", 21.0, 21),
    ]
    aware = BotProfile("aware", reach=0.0, value_weight=0.0, need_weight=0.0, bye_aware=1.0)
    blind = BotProfile("blind", reach=0.0, value_weight=0.0, need_weight=0.0, bye_aware=0.0)
    assert _one_pick(aware, board, prior=("A", "B"))["canonical_id"] == "D"
    assert _one_pick(blind, board, prior=("A", "B"))["canonical_id"] == "C"


def test_reachers_stray_further_from_market_than_market_bots():
    def deviation(profile):
        _, picks, _ = _run_room([profile] * 4, num_teams=4, rounds=8, seed=11)
        return sum(abs(row["adp"] - pick_no) for pick_no, _, _, row in picks) / len(picks)

    market = BotProfile("m", reach=0.2, value_weight=0.0, need_weight=0.3, run_sensitivity=0.0)
    reacher = BotProfile("r", reach=1.5, value_weight=0.0, need_weight=0.3, run_sensitivity=0.0)
    assert deviation(reacher) > deviation(market)


def test_qb_early_bot_reaches_for_the_elite_qb():
    board = _board()
    early = BotProfile("qb early", reach=0.0, value_weight=0.0, need_weight=0.5, elite_qb=1.0, pos_bias={"QB": 1.5})
    market = BotProfile("market", reach=0.0, value_weight=0.0, need_weight=0.5)
    # Pick 3: RB1/WR1 gone; the market bot takes RB2 (adp 5) over QB1 (adp 3)?
    # No — QB1 is next by market. Make it a real reach: bump QB1 to adp 7.
    for r in board:
        if r["canonical_id"] == "QB1":
            r["adp"], r["overall_rank"] = 7.0, 7
    assert _one_pick(early, board, external=("RB1", "WR1"), pick_no=3)["canonical_id"] == "QB1"
    assert _one_pick(market, board, external=("RB1", "WR1"), pick_no=3)["canonical_id"] != "QB1"


def test_value_hunter_drafts_the_board_not_the_market():
    board = [
        _row("Hyped", "WR", 1.0, 5),   # market loves him, board says #5
        _row("Sleeper", "RB", 6.0, 1),  # board's #1, market has him 6th
        _row("X1", "RB", 2.0, 2), _row("X2", "WR", 3.0, 3), _row("X3", "TE", 4.0, 4),
        _row("QB1", "QB", 8.0, 8),
    ]
    hunter = BotProfile("hunter", reach=0.0, value_weight=1.0, need_weight=0.0)
    market = BotProfile("market", reach=0.0, value_weight=0.0, need_weight=0.0)
    assert _one_pick(hunter, board, pick_no=1, round_no=1)["canonical_id"] == "Sleeper"
    assert _one_pick(market, board, pick_no=1, round_no=1)["canonical_id"] == "Hyped"
