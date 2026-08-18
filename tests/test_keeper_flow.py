"""Full keeper-league flow on top of manual entry.

* the cost-round rule (Rd R → R−3, rounds 1–3 un-keepable, undrafted → 15
  then 14/16/17, max 4, no two keepers on one round);
* the store-backed KeeperBook (add / re-derive / remove / duplicate guard);
* applying keepers to the pick list: pool exclusion from pick 1, cost-round
  picks occupied (no live selection there), my next/after picks skip them,
  keepers land on the right rosters and shape every team's needs;
* the survival model's roster-need scale (teams that don't need a position
  make its players safer);
* league snapshot: every team's roster + needs + next pick;
* HTTP keeper routes end to end; state (picks + keepers) survives a restart.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

from fantasy_coach.draft.loop import DraftLoop
from fantasy_coach.draft.manual import KeeperBook, ManualDraft, ManualPickSource, PlayerFinder
from fantasy_coach.draft.recommend import NEED_WEIGHTS
from fantasy_coach.draft.survival import (
    NEED_SCALE_MAX,
    NEED_SCALE_MIN,
    estimate_survival,
    need_scale,
)
from fantasy_coach.draft.web import CompanionServer
from fantasy_coach.league import KeeperConflict, KeeperRules, assign_keeper_rounds, keeper_cost_round
from tests.conftest import DRAFT_LEAGUE_KEY, FakeClock

T1, T2 = f"{DRAFT_LEAGUE_KEY}.t.1", f"{DRAFT_LEAGUE_KEY}.t.2"
RULES = KeeperRules()  # the founder's league: max 4, keep from Rd 4, cost −3, undrafted 15


# -- the rule -----------------------------------------------------------------


def test_cost_round_rule():
    assert keeper_cost_round(9, RULES) == 6  # 2025 Rd 9 → 2026 Rd 6
    assert keeper_cost_round(4, RULES) == 1
    assert keeper_cost_round(None, RULES) == 15
    with pytest.raises(KeeperConflict, match="can't be kept"):
        keeper_cost_round(3, RULES)


def test_assign_rounds_spreads_undrafted_and_guards_conflicts():
    got = dict(assign_keeper_rounds([("a", 9), ("b", None), ("c", None), ("d", None)], RULES))
    assert got == {"a": 6, "b": 15, "c": 14, "d": 16}
    got = dict(assign_keeper_rounds([("a", None), ("b", None), ("c", None), ("d", None)], RULES))
    assert sorted(got.values()) == [14, 15, 16, 17]
    with pytest.raises(KeeperConflict, match="allow 4"):
        assign_keeper_rounds([("a", None)] * 5, RULES)
    with pytest.raises(KeeperConflict, match="both cost round"):
        assign_keeper_rounds([("a", 9), ("b", 9)], RULES)
    # An undrafted keeper never lands on a round a drafted keeper took.
    got = dict(assign_keeper_rounds([("a", 18), ("b", None)], RULES))
    assert got == {"a": 15, "b": 14}


# -- the book -----------------------------------------------------------------


def test_keeper_book_persists_rederives_and_guards(draft_store):
    book = KeeperBook(draft_store, DRAFT_LEAGUE_KEY, RULES, rounds=17)
    book.add(T1, "R1", name="Rusher One", position="RB", last_round=9)
    book.add(T1, "W1", name="Wideout One", position="WR", last_round=None)
    book.add(T1, "W2", name="Wideout Two", position="WR", last_round=None)
    rows = {r["canonical_id"]: r for r in book.by_team()[T1]}
    assert rows["R1"]["cost_round"] == 6 and rows["W1"]["cost_round"] == 15 and rows["W2"]["cost_round"] == 14
    with pytest.raises(ValueError, match="already kept"):
        book.add(T2, "R1", last_round=None)
    with pytest.raises(KeeperConflict):
        book.add(T1, "Q1", last_round=2)
    assert book.remove(T1, "W1") == 1
    rows = {r["canonical_id"]: r for r in book.by_team()[T1]}
    assert rows["W2"]["cost_round"] == 15  # re-derived: the survivor moves back to 15
    assert sorted(book.triples()) == [(T1, 6, "R1"), (T1, 15, "W2")]
    assert draft_store.keepers(DRAFT_LEAGUE_KEY)[0]["source"] == "rule"
    book.add(T1, "T1", name="Tightend One", position="TE", last_round=None, cost_round=12)  # commissioner override
    assert {r["canonical_id"]: r["source"] for r in book.by_team()[T1]}["T1"] == "override"


# -- the pick list ------------------------------------------------------------


def test_apply_keepers_occupies_cost_rounds_and_reports_conflicts():
    src = ManualPickSource([T1, T2], rounds=4)
    src.mark("R1")  # live pick 1
    warns = src.apply_keepers([(T2, 2, "W1"), (T1, 1, "R2"), (T1, 9, "X")])
    # T1's round 1 = pick 1 is already a live pick; round 9 doesn't exist.
    assert any("already holds a live pick" in w for w in warns) and any("no round 9" in w for w in warns)
    assert src.keeper_pick_numbers == {3}  # T2 round 2 = pick 3
    assert src.made_picks()[1].player_id == "W1"
    src.apply_keepers([(T2, 3, "W1")])  # moved to round 3 → pick 6
    assert src.keeper_pick_numbers == {6} and src.next_open() == 2


def _league(draft_store, draft_settings, keepers):
    src = ManualPickSource([T1, T2], rounds=8, game_code="sim", keepers=keepers)
    loop = DraftLoop(
        draft_store, draft_settings, src, my_team_key=T1, mode="manual",
        record_to_store=True, draft_order=[T1, T2], team_names={T1: "You", T2: "Rival"},
        keeper_rules=RULES, time_func=FakeClock().time, sleep_func=lambda s: None,
    )
    finder = PlayerFinder(dict(r) for r in draft_store.sql("SELECT canonical_id, name, position, team FROM players"))
    book = KeeperBook(draft_store, DRAFT_LEAGUE_KEY, RULES, rounds=8)
    ctl = ManualDraft(source=src, loop=loop, finder=finder, team_names={T1: "You", T2: "Rival"}, keepers=book)
    return ctl, loop


def test_keepers_shape_pool_rosters_needs_and_my_pick_sequence(draft_store, draft_settings):
    # Rival keeps R1 (round 2 → pick 3) and W1 (round 4 → pick 7); I keep T1 (round 3 → pick 5).
    ctl, loop = _league(draft_store, draft_settings, [(T2, 2, "R1"), (T2, 4, "W1"), (T1, 3, "T1")])
    s = loop.poll_once()
    names = {p["name"] for p in s["available"]}
    assert not {"Rusher One", "Wideout One", "Tightend One"} & names  # gone from pick 1
    assert s["available_count"] == 16
    assert s["draft"]["pick_count"] == 3 and s["draft"]["current_pick"] == 1
    # My picks: 1, then 4 (pick 5 = my keeper is skipped), then 8 …
    assert (s["draft"]["my_next_pick"], s["draft"]["my_pick_after"]) == (1, 4)
    teams = {t["name"]: t for t in s["teams"]}
    assert [r["player"]["name"] for r in teams["Rival"]["roster"] if r["player"]] == ["Rusher One", "Wideout One"]
    assert teams["Rival"]["needs"]["open_starters"] == {"QB": 1, "RB": 1, "WR": 1, "TE": 1}
    assert teams["Rival"]["next_pick"] == 2
    assert "TE" not in teams["You"]["needs"]["open_starters"]  # my keeper filled it
    assert any(sl["player"] and sl["player"]["name"] == "Tightend One" for sl in s["roster"])
    # The recommendation is off the reduced pool and never a kept player.
    assert s["recommendation"]["name"] == "Rusher Two"
    # The board itself was built on the post-keeper pool (kept players are not
    # entries, so baselines/VORP come from what is actually draftable).
    assert not {"R1", "W1", "T1"} & {e.canonical_id for e in loop.board.entries}
    assert loop.board.baselines["TE"] == 110.0  # T1 gone → TE replacement is now T3, not T2
    # Live entry: my pick 1, Rival's pick 2 → the clock jumps to 4 (3 is Rival's keeper).
    ctl.mark("R2")
    s = ctl.mark("W2")["state"]
    assert s["draft"]["current_pick"] == 4 and s["draft"]["on_the_clock"]["is_me"]
    rival = {t["name"]: t for t in s["teams"]}["Rival"]
    assert sorted(r["player"]["name"] for r in rival["roster"] if r["player"]) == ["Rusher One", "Wideout One", "Wideout Two"]


def test_keeper_entry_via_controller_persists_and_applies_live(draft_store, draft_settings):
    ctl, loop = _league(draft_store, draft_settings, [])
    loop.poll_once()
    out = ctl.add_keeper(T2, "R1", last_round=9)
    assert out["ok"] and out["keepers"]["enabled"]
    kept = {t["team_key"]: t["keepers"] for t in out["keepers"]["teams"]}[T2]
    assert kept[0]["name"] == "Rusher One" and kept[0]["cost_round"] == 6
    s = out["state"]
    assert not any(p["name"] == "Rusher One" for p in s["available"])
    assert draft_store.keepers(DRAFT_LEAGUE_KEY)[0]["cost_round"] == 6
    rival = {t["name"]: t for t in s["teams"]}["Rival"]
    assert [r["player"]["name"] for r in rival["roster"] if r["player"]] == ["Rusher One"]
    assert rival["roster"][1]["player"]["keeper"].startswith("keeper")
    out = ctl.remove_keeper(T2, "R1")
    assert any(p["name"] == "Rusher One" for p in out["state"]["available"])
    assert draft_store.keepers(DRAFT_LEAGUE_KEY) == []


def test_restart_restores_keepers_and_picks(draft_store, draft_settings):
    ctl, loop = _league(draft_store, draft_settings, [])
    loop.poll_once()
    ctl.add_keeper(T2, "R1", last_round=9)  # pick 12 (T2 round 6)
    ctl.mark("R2")  # pick 1 (me)
    ctl.mark("W1")  # pick 2 (rival)
    # New process: rebuild from the store's keepers + picks.
    keepers = [(str(r["team_key"]), int(r["cost_round"]), str(r["canonical_id"])) for r in draft_store.keepers(DRAFT_LEAGUE_KEY)]
    assert keepers == [(T2, 6, "R1")]
    src2 = ManualPickSource([T1, T2], rounds=8, game_code="sim", keepers=keepers)
    assert src2.restore(draft_store.draft_picks(DRAFT_LEAGUE_KEY)) == 2  # the two live picks (keeper already there)
    loop2 = DraftLoop(draft_store, draft_settings, src2, my_team_key=T1, mode="manual", record_to_store=True,
                      draft_order=[T1, T2], team_names={T1: "You", T2: "Rival"},
                      time_func=FakeClock().time, sleep_func=lambda s: None)
    s = loop2.poll_once()
    assert s["draft"]["current_pick"] == 3 and s["draft"]["pick_count"] == 3
    rival = {t["name"]: t for t in s["teams"]}["Rival"]
    assert sorted(r["player"]["name"] for r in rival["roster"] if r["player"]) == ["Rusher One", "Wideout One"]


# -- survival: roster needs -------------------------------------------------------


def test_need_scale_and_its_effect_on_survival():
    room = [{"RB": 1.0}, {"RB": 1.0}, {"RB": 0.55}, {"RB": 0.55}]  # two teams set at RB
    assert need_scale([{"RB": 0.55}, {"RB": 0.55}], room, ["RB"])["RB"] == pytest.approx(0.55 / 0.775)
    assert need_scale([{"RB": 1.0}], room, ["RB"])["RB"] == pytest.approx(1.0 / 0.775)
    assert need_scale([], room, ["RB"])["RB"] == 1.0
    assert need_scale([{"RB": 0.0}], room, ["RB"])["RB"] == NEED_SCALE_MIN
    assert need_scale([{"RB": 9.0}], room, ["RB"])["RB"] == NEED_SCALE_MAX
    player = [{"canonical_id": "x", "position": "RB", "adp": 8.0, "overall_rank": 3}]
    base = estimate_survival(player, current_pick=5, my_next_pick=12, my_pick_after=20)["x"]
    safe = estimate_survival(player, current_pick=5, my_next_pick=12, my_pick_after=20,
                             need_scale_next={"RB": 0.5}, need_scale_after={"RB": 0.5})["x"]
    hot = estimate_survival(player, current_pick=5, my_next_pick=12, my_pick_after=20,
                            need_scale_next={"RB": 1.6})["x"]
    assert safe.p_next > base.p_next > hot.p_next
    assert safe.p_after > base.p_after and hot.p_after == base.p_after  # only "next" was scaled


def test_loop_feeds_real_rosters_into_survival(draft_store, draft_settings):
    # Rival's RB slots are both kept-full → RBs are safer to my next pick; QB is not.
    ctl, loop = _league(draft_store, draft_settings, [(T2, 5, "R1"), (T2, 6, "R2")])
    loop.poll_once()
    ctl.mark("W1")  # my pick 1 → Rival on the clock at 2, my next is 4
    scale = loop._need_scale_next
    # Rival's RB need is only the flex now (0.85); mine is an open starter (1.0).
    assert scale["RB"] == pytest.approx(NEED_WEIGHTS["flex"] / ((NEED_WEIGHTS["flex"] + NEED_WEIGHTS["starter"]) / 2))
    assert scale["QB"] == pytest.approx(1.0)
    ctl_plain, loop_plain = _league(draft_store, draft_settings, [])
    loop_plain.poll_once()
    ctl_plain.mark("W1")
    s_k = loop.snapshot()
    s_p = loop_plain.snapshot()
    rb_k = next(p for p in s_k["available"] if p["name"] == "Rusher Three")["survival"]["p_next"]
    rb_p = next(p for p in s_p["available"] if p["name"] == "Rusher Three")["survival"]["p_next"]
    assert rb_k > rb_p


# -- HTTP ----------------------------------------------------------------------


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_http_keeper_routes(draft_store, draft_settings):
    ctl, loop = _league(draft_store, draft_settings, [])
    loop.poll_once()
    with CompanionServer(loop.snapshot, port=0, manual=ctl) as srv:
        with urllib.request.urlopen(srv.url + "api/keepers") as r:
            view = json.loads(r.read())
        assert view["enabled"] and view["rules"]["max_keepers"] == 4
        status, out = _post(srv.url + "api/keepers/add", {"team_key": T2, "player": "R1", "last_round": 9})
        assert status == 200 and out["ok"] and out["state"]["available_count"] == 18
        status, out = _post(srv.url + "api/keepers/add", {"team_key": T2, "player": "R2", "last_round": 2})
        assert status == 400 and "can't be kept" in out["error"]
        status, out = _post(srv.url + "api/keepers/add", {"team_key": T1, "player": "R1", "last_round": None})
        assert status == 400 and "already kept" in out["error"]
        status, out = _post(srv.url + "api/keepers/remove", {"team_key": T2, "player": "R1"})
        assert status == 200 and out["state"]["available_count"] == 19
