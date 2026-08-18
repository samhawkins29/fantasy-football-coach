"""The offline league spec (``data/league.json``): the founder's exact rules.

Covers: parsing into LeagueSettings (full PPR, IDP ``D`` slot as a flex over
DL/LB/DB, no kickers, TE flex-only, playoffs 15–17), the checked-in real
spec, IDP position normalization + projection keys, replacement baselines
under that lineup across 10 teams, unstartable kickers dropped, week 18
valueless, keeper resolution/scripting/pick-slot handling, and the keeper
note on the recommendation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fantasy_coach.clients.models import DraftPick, RosterPosition
from fantasy_coach.draft.loop import DraftLoop
from fantasy_coach.draft.simulate import script_draft
from fantasy_coach.ingest.names import normalize_position
from fantasy_coach.ingest.projections import PROJECTED_STAT_KEYS, REFERENCE_SCORING
from fantasy_coach.ingest.sources import ProjectionRecord
from fantasy_coach.league import (
    KeeperRules,
    keeper_note,
    load_league_spec,
    resolve_keepers,
    team_key_for_slot,
)
from fantasy_coach.value.board import build_value_board, starter_demand, startable_positions
from fantasy_coach.value.scoring import league_scoring
from fantasy_coach.value.schedule import playoff_weeks
from tests.conftest import DRAFT_LEAGUE_KEY, FakeClock, make_pick

REAL_SPEC = Path(__file__).resolve().parents[1] / "data" / "league.json"


# -- parsing ------------------------------------------------------------------


def test_real_spec_parses_to_sams_league():
    spec = load_league_spec(REAL_SPEC)
    s = spec.settings
    assert spec.num_teams == 10 and spec.rounds == 17
    assert s.playoff_start_week == 15 and s.num_playoff_teams == 6
    assert playoff_weeks(s) == [15, 16, 17]
    scoring = league_scoring(s)
    assert scoring["rec"] == 1.0  # full PPR
    assert scoring["pass_td"] == 4.0 and scoring["tackle_solo"] == 1.0
    assert startable_positions(s) == {"QB", "RB", "WR", "TE", "DEF", "DL", "LB", "DB"}
    assert "K" not in startable_positions(s)
    dedicated, flex = starter_demand(s, 10)
    assert dedicated == {"QB": 10, "RB": 20, "WR": 20, "DEF": 10}
    assert sorted(flex) == [(("DL", "LB", "DB"), 10), (("WR", "RB", "TE"), 20)]
    assert spec.is_keeper_league and spec.keeper_rules.max_keepers == 4
    assert spec.regular_season_weeks == 14 and spec.playoff_byes == 2
    assert spec.draft_date.startswith("2026-09-04")


def test_idp_slot_is_a_flex_and_subpositions_collapse():
    assert RosterPosition(position="D").is_flex
    assert RosterPosition(position="D").flex_positions == ["DL", "LB", "DB"]
    assert normalize_position("OLB") == "LB" and normalize_position("de") == "DL"
    assert normalize_position("FS") == "DB" and normalize_position("CB") == "DB"
    assert normalize_position("DST") == "DEF" and normalize_position("K") == "K"
    for key in ("tackle_solo", "tackle_ast", "sack", "def_int", "pass_def"):
        assert key in PROJECTED_STAT_KEYS and key in REFERENCE_SCORING


def test_spec_rejects_unknown_scoring_keys(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text('{"league_key": "x.l.1", "num_teams": 10, "scoring": {"bogus": 1}}', encoding="utf-8")
    with pytest.raises(ValueError, match="unknown stat keys"):
        load_league_spec(p)


# -- the board under this lineup ---------------------------------------------


def _rec(gid, pos, points, team="KC"):
    return ProjectionRecord(source="t", source_id=gid, source_id_field="gsis_id",
                            points=points, position=pos, team=team, name=gid, stats={})


def test_baselines_follow_sams_lineup_across_ten_teams():
    spec = load_league_spec(REAL_SPEC)
    recs = []
    for i in range(1, 41):
        recs.append(_rec(f"RB{i}", "RB", 300.0 - 4 * i))
        recs.append(_rec(f"WR{i}", "WR", 300.0 - 4 * i))
    recs += [_rec(f"TE{i}", "TE", 250.0 - 10 * i) for i in range(1, 16)]
    recs += [_rec(f"QB{i}", "QB", 400.0 - 10 * i) for i in range(1, 21)]
    recs += [_rec(f"LB{i}", "LB", 120.0 - 3 * i) for i in range(1, 21)]
    recs += [_rec(f"DL{i}", "DL", 100.0 - 3 * i) for i in range(1, 21)]
    recs += [_rec(f"DB{i}", "DB", 105.0 - 3 * i) for i in range(1, 21)]
    recs += [_rec(f"K{i}", "K", 150.0 - i) for i in range(1, 6)]
    board = build_value_board(recs, spec.settings)
    b = board.baselines
    # 10 QB starters → QB11 (290). 20 RB + 20 WR dedicated, then 20 flex
    # greedy across RB/WR/TE: TE1..TE? compete — the flex drains the top of
    # the RB/WR/TE pools by points, so all three baselines land together.
    assert b["QB"] == 290.0
    assert b["RB"] == b["WR"]  # symmetric pools → same replacement
    assert b["RB"] < 300.0 - 4 * 20  # flex pushed the baseline past RB20
    assert b["TE"] <= 250.0 - 10 * 1  # TE only via flex: baseline near the top TE
    # One IDP slot × 10 teams: the flex greedily takes the ten best IDPs by
    # points across DL/LB/DB (LB 117…, DB 102…, DL 97…): the 10th-best is
    # DL1 (97) — so exactly one DL goes and DL's baseline is DL2 (94); LB
    # and DB drain to within a step of each other.
    assert b["DL"] == 94.0
    assert b["LB"] < 120.0 - 3 * 5 and b["DB"] < 102.0
    assert abs(b["LB"] - b["DB"]) <= 3.0  # greedy flex equalizes the pools
    assert board.skipped_unstartable == 5  # kickers dropped in a no-K league
    assert not any(e.position == "K" for e in board.entries)
    assert board.num_teams == 10


def test_week_18_carries_no_value():
    from fantasy_coach.ingest.schedule import SeasonSchedule
    from tests.test_playoff_value import team_season

    spec = load_league_spec(REAL_SPEC)
    sched = SeasonSchedule(
        season=2026,
        opponents={"AAA": {**team_season(), 18: "NEU"}, "BBB": {**team_season(), 18: "NEU"}},
        byes={"AAA": 9, "BBB": 9},
        multipliers={"RB": {"NEU": 1.0}},
    )
    board = build_value_board(
        [_rec("A", "RB", 340.0, "AAA"), _rec("B", "RB", 170.0, "BBB")],
        spec.settings, num_teams=1, schedule=sched, sos_weight=1.0,
    )
    a = next(e for e in board.entries if e.name == "A")
    assert 18 not in a.week_multipliers and max(a.week_multipliers) == 17
    assert len(a.week_multipliers) == 16  # 17 weeks minus the bye
    assert board.playoff_weeks == [15, 16, 17]


# -- keepers ------------------------------------------------------------------


def _spec_with_keepers(tmp_path, keepers, league_key=DRAFT_LEAGUE_KEY):
    import json

    payload = {
        "league_key": league_key, "num_teams": 2,
        "scoring": {"rec": 0.5},
        "roster": [{"position": "QB", "count": 1}, {"position": "RB", "count": 2},
                   {"position": "WR", "count": 2}, {"position": "TE", "count": 1},
                   {"position": "W/R/T", "count": 1}, {"position": "BN", "count": 1}],
        "draft": {"rounds": 8, "my_slot": 1},
        "keeper_rules": {"max_keepers": 4, "min_draft_round_to_keep": 4,
                         "cost_rounds_earlier": 3, "undrafted_cost_round": 15},
        "keepers": keepers,
    }
    p = tmp_path / "league.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return load_league_spec(p)


def test_keepers_resolve_by_name_or_id_with_warnings(tmp_path, draft_store):
    spec = _spec_with_keepers(tmp_path, {
        "1": [{"player": "Rusher One", "round": 4}],
        "2": [{"player": "W3", "round": 6}, {"player": "Nobody Real", "round": 7}],
    })
    players = draft_store.sql("SELECT canonical_id, name, position FROM players")
    resolved, warns = resolve_keepers(spec, players)
    assert [(k.team_key, k.round, k.canonical_id) for k in resolved] == [
        (team_key_for_slot(DRAFT_LEAGUE_KEY, 1), 4, "R1"),
        (team_key_for_slot(DRAFT_LEAGUE_KEY, 2), 6, "W3"),
    ]
    assert any("Nobody Real" in w for w in warns)


def test_keeper_note_follows_the_rules():
    rules = KeeperRules()
    assert "not keeper-eligible" in keeper_note(3, rules)
    assert keeper_note(9, rules) == "Keepable next year at a round-6 cost (drafted round 9)"
    assert keeper_note(4, rules).endswith("round-1 cost (drafted round 4)")
    assert keeper_note(5, None) == ""


def test_scripted_draft_places_keepers_in_their_cost_round(draft_store, draft_settings):
    t1, t2 = f"{DRAFT_LEAGUE_KEY}.t.1", f"{DRAFT_LEAGUE_KEY}.t.2"
    script = script_draft(
        draft_store, draft_settings, keepers=[(t1, 4, "R1"), (t2, 3, "W1")]
    )
    by_pick = {p.pick: p for p in script}
    # 2-team snake: t1 picks 1,4,5,8,…; round 4 for t1 is pick 8; t2 round 3 = pick 6.
    assert by_pick[8].team_key == t1 and by_pick[8].player_id == "201"  # R1's yahoo id
    assert by_pick[6].team_key == t2 and by_pick[6].player_id == "301"
    ids = [p.player_id for p in script]
    assert ids.count("201") == 1 and ids.count("301") == 1  # never drafted twice
    assert ids[0] != "201"  # the keeper is NOT taken at pick 1


def test_loop_treats_pre_made_keeper_picks_as_not_on_the_clock(draft_store, draft_settings):
    from tests.test_draft_loop import ListSource

    me, opp = f"{DRAFT_LEAGUE_KEY}.t.1", f"{DRAFT_LEAGUE_KEY}.t.2"
    order = [me, opp]

    def team_for(n):
        rnd, idx = divmod(n - 1, 2)
        return order[idx] if rnd % 2 == 0 else order[1 - idx]

    # Yahoo shape: 16 picks, my round-4 keeper (pick 8 = R1) already made.
    picks = []
    for n in range(1, 17):
        if n == 8:
            picks.append(make_pick(8, me, "201"))
        else:
            p = DraftPick(pick=n, round=(n - 1) // 2 + 1)
            picks.append(p)
    loop = DraftLoop(
        draft_store, draft_settings, ListSource(picks), my_team_key=me,
        mode="simulation", draft_order=order, keeper_rules=KeeperRules(),
        time_func=FakeClock().time, sleep_func=lambda s: None,
    )
    snap = loop.poll_once()
    d = snap["draft"]
    assert d["current_pick"] == 1  # first UNMADE pick, not made+1 (=2)
    assert d["my_next_pick"] == 1 and d["my_pick_after"] == 4  # pick 8 skipped
    assert loop.recommendation.player.entry.canonical_id != "R1"  # kept → out of pool
    assert any(s["player"] and s["player"]["name"] == "Rusher One" for s in snap["roster"])
    assert any("not keeper-eligible" in r for r in snap["recommendation"]["reasons"])
