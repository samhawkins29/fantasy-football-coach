"""Manual draft entry — the offline live-draft path.

Covers: the hand-fed pick source (snake geometry, keepers pre-made, mark /
unmark / undo / reset, duplicate and slot guards, restore from stored picks,
optional Yahoo overlay), fuzzy search-as-you-type (prefix, in-order token
prefixes, initials / "cmc", misspellings; available first; taken flagged),
the loop reacting live (pool shrinks, recommendation and survival move, undo
returns the player, snake position → "your pick in N / likely gone"), the
store persisting every change and a fresh loop resuming from it, and the
HTTP routes end to end.
"""

from __future__ import annotations

import json
import urllib.request

import pytest

from fantasy_coach.clients.models import DraftPick
from fantasy_coach.draft.loop import DraftLoop
from fantasy_coach.draft.manual import (
    ManualDraft,
    ManualPickSource,
    PlayerFinder,
    snake_team_keys,
)
from fantasy_coach.draft.web import CompanionServer
from tests.conftest import DRAFT_LEAGUE_KEY, FakeClock

T1, T2 = f"{DRAFT_LEAGUE_KEY}.t.1", f"{DRAFT_LEAGUE_KEY}.t.2"


# -- the pick source ----------------------------------------------------------


def test_snake_geometry_and_pick_numbers():
    src = ManualPickSource([T1, T2], rounds=3)
    assert snake_team_keys([T1, T2], 3) == [T1, T2, T2, T1, T1, T2]
    assert src.total == 6 and src.next_open() == 1
    assert src.pick_number_for(T1, 2) == 4 and src.pick_number_for(T2, 3) == 6
    assert src.team_for(3) == T2


def test_mark_defaults_to_the_clock_and_advances():
    src = ManualPickSource([T1, T2], rounds=2)
    p = src.mark("R1")
    assert (p.pick, p.team_key, p.player_id) == (1, T1, "R1")
    assert src.next_open() == 2
    p2 = src.mark("W1", team_key=T1)  # trade: T1 owns pick 2 in the real room
    assert p2.pick == 2 and p2.team_key == T1
    fetched = src.fetch()
    assert [x.is_made for x in fetched] == [True, True, False, False]
    assert fetched[1].player_key.endswith(".p.W1")


def test_guards_slot_taken_out_of_range_and_complete():
    src = ManualPickSource([T1, T2], rounds=1)
    src.mark("R1")
    with pytest.raises(ValueError, match="already made"):
        src.mark("R2", pick_no=1)
    with pytest.raises(ValueError, match="outside"):
        src.mark("R2", pick_no=9)
    src.mark("R2")
    with pytest.raises(ValueError, match="complete"):
        src.mark("R3")


def test_remarking_a_player_moves_him_instead_of_duplicating():
    src = ManualPickSource([T1, T2], rounds=2)
    src.mark("R1")
    src.mark("R1", pick_no=3)
    made = src.made_picks()
    assert [p.pick for p in made] == [3] and src.next_open() == 1


def test_unmark_undo_reset_and_keepers():
    src = ManualPickSource([T1, T2], rounds=3, keepers=[(T2, 2, "K9")])
    assert src.made_picks()[0].pick == 3 and src.made_picks()[0].player_id == "K9"
    src.mark("R1")  # pick 1
    src.mark("R2")  # pick 2
    src.mark("W1")  # pick 4 (3 is the keeper)
    assert src.next_open() == 5
    assert src.undo().pick == 4  # most recently marked, not highest number
    assert src.unmark(1).player_id == "R1" and src.next_open() == 1
    assert src.unmark(1) is None  # already open
    assert src.undo().pick == 2  # history skips the cleared pick
    src.mark("R1")
    src.reset()
    assert [p.pick for p in src.made_picks()] == [3]  # keeper survives a reset


def test_restore_reloads_made_picks_from_stored_shape():
    src = ManualPickSource([T1, T2], rounds=2)
    stored = [DraftPick(pick=1, round=1, team_key=T1, player_key="sim.p.R1"),
              DraftPick(pick=2, round=1, team_key=T2, player_key="sim.p.W1"),
              DraftPick(pick=3, round=2, team_key=T2)]  # unmade rows are ignored
    assert src.restore(stored) == 2
    assert src.next_open() == 3 and src.picks_by_player() == {"R1": 1, "W1": 2}
    assert src.undo().pick == 2  # restored picks are undoable too


def test_overlay_fills_only_open_slots_and_never_breaks_manual():
    src = ManualPickSource([T1, T2], rounds=2)
    src.mark("R1")

    class Yahoo:
        def __init__(self, picks, fail=False):
            self.picks, self.fail = picks, fail

        def fetch(self):
            if self.fail:
                raise ConnectionError("no api")
            return self.picks

    src.overlay(Yahoo([DraftPick(pick=1, round=1, team_key=T1, player_key="y.p.ZZ"),
                       DraftPick(pick=2, round=1, team_key=T2, player_key="y.p.W1")]))
    picks = src.fetch()
    assert picks[0].player_id == "R1"  # manual wins
    assert picks[1].player_id == "W1"  # overlay filled the open slot
    src.overlay(Yahoo([], fail=True))
    assert [p.player_id for p in src.fetch() if p.is_made] == ["R1", "W1"]  # unaffected


# -- search -------------------------------------------------------------------


@pytest.fixture
def finder(draft_store):
    rows = draft_store.sql(
        "SELECT p.canonical_id, p.name, p.position, p.team, b.overall_rank "
        "FROM players p LEFT JOIN value_board b ON b.canonical_id = p.canonical_id"
    )
    return PlayerFinder(
        {"canonical_id": r["canonical_id"], "name": r["name"], "position": r["position"],
         "team": r["team"], "overall_rank": r["overall_rank"], "raw_id": r["canonical_id"]}
        for r in rows
    )


def test_search_prefix_tokens_initials_and_typos(finder):
    names = lambda q, **kw: [r["name"] for r in finder.search(q, **kw)]  # noqa: E731
    assert names("rush")[:2] == ["Rusher One", "Rusher Two"]  # board order within tier
    assert names("r two")[0] == "Rusher Two"  # in-order token prefixes
    assert names("two")[0] in ("Rusher Two", "Wideout Two", "Quincy Two", "Tightend Two")
    assert names("qo")[0] == "Quincy One"  # initials
    assert names("rone")[0] == "Rusher One"  # "cmc"-style: initial + last-name prefix
    assert names("rushr one")[0] == "Rusher One"  # typo tolerated
    assert names("zzzz") == []
    assert names("wideout", position="WR") and all(r["position"] == "WR" for r in finder.search("wideout", position="WR"))


def test_search_puts_available_first_and_flags_taken(finder):
    res = finder.search("rusher", taken={"R1": 1})
    assert res[0]["name"] == "Rusher Two" and res[0]["available"]
    gone = next(r for r in res if r["name"] == "Rusher One")
    assert gone["available"] is False and gone["pick"] == 1


# -- the loop, live -----------------------------------------------------------


def _manual(draft_store, draft_settings, *, keepers=(), record=False):
    src = ManualPickSource([T1, T2], rounds=8, game_code="sim", keepers=keepers)
    clock = FakeClock()
    loop = DraftLoop(
        draft_store, draft_settings, src, my_team_key=T1, mode="manual",
        record_to_store=record, draft_order=[T1, T2],
        team_names={T1: "You", T2: "Rival"},
        time_func=clock.time, sleep_func=clock.sleep,
    )
    rows = draft_store.sql("SELECT canonical_id, name, position, team FROM players")
    finder = PlayerFinder(dict(r) for r in rows)
    return ManualDraft(source=src, loop=loop, finder=finder, team_names={T1: "You", T2: "Rival"}), loop


def test_marking_shrinks_the_pool_and_moves_the_recommendation(draft_store, draft_settings):
    ctl, loop = _manual(draft_store, draft_settings)
    s0 = loop.poll_once()
    assert s0["recommendation"]["name"] == "Rusher One" and s0["available_count"] == 19
    assert s0["draft"]["on_the_clock"]["is_me"] and s0["draft"]["my_pick_after"] == 4
    out = ctl.mark("R1")  # my pick 1
    s1 = out["state"]
    assert out["ok"] and s1["available_count"] == 18
    assert s1["draft"]["current_pick"] == 2 and s1["draft"]["on_the_clock"]["team"] == "Rival"
    assert s1["draft"]["my_next_pick"] == 4 and s1["draft"]["picks_until_mine"] == 2
    assert s1["recommendation"]["name"] != "Rusher One"
    assert all(p["name"] != "Rusher One" for p in s1["available"])
    # roster-need logic saw MY pick: an RB slot is filled.
    assert any(sl["player"] and sl["player"]["name"] == "Rusher One" for sl in s1["roster"])
    # survival is live: every available player carries p_next to my pick 4.
    assert all(p["survival"]["p_next"] is not None for p in s1["available"])
    assert isinstance(s1["likely_gone"], list)


def test_undo_returns_the_player_and_unmark_reopens_the_slot(draft_store, draft_settings):
    ctl, loop = _manual(draft_store, draft_settings)
    loop.poll_once()
    ctl.mark("R1")
    ctl.mark("R2", team_key=T2)
    s = ctl.mark("W1")["state"]
    assert s["available_count"] == 16 and s["draft"]["current_pick"] == 4
    s = ctl.undo()["state"]  # W1 back
    assert s["available_count"] == 17 and any(p["name"] == "Wideout One" for p in s["available"])
    s = ctl.unmark(1)["state"]  # clear my mis-entered pick 1 → clock returns to pick 1
    assert s["draft"]["current_pick"] == 1 and s["available_count"] == 18
    assert not any(sl["player"] and sl["player"]["name"] == "Rusher One" for sl in s["roster"])
    assert ctl.undo()["message"] in ("undid pick 2",)  # history skips the cleared one
    assert ctl.undo()["message"] == "nothing to undo"


def test_state_persists_in_the_store_and_a_new_loop_resumes(draft_store, draft_settings):
    ctl, loop = _manual(draft_store, draft_settings, record=True)
    loop.poll_once()
    ctl.mark("R1")
    ctl.mark("W1")
    ctl.mark("R2")
    stored = draft_store.draft_picks(DRAFT_LEAGUE_KEY)
    assert [(p.pick, p.player_id, p.team_key) for p in stored] == [(1, "R1", T1), (2, "W1", T2), (3, "R2", T2)]
    ctl.undo()
    assert [p.pick for p in draft_store.draft_picks(DRAFT_LEAGUE_KEY)] == [1, 2]  # mirror is rebuild-not-append

    # "Crash": build a brand-new source + loop and restore from the store.
    src2 = ManualPickSource([T1, T2], rounds=8, game_code="sim")
    assert src2.restore(draft_store.draft_picks(DRAFT_LEAGUE_KEY)) == 2
    loop2 = DraftLoop(draft_store, draft_settings, src2, my_team_key=T1, mode="manual",
                      record_to_store=True, draft_order=[T1, T2],
                      time_func=FakeClock().time, sleep_func=lambda s: None)
    s = loop2.poll_once()
    assert s["draft"]["current_pick"] == 3 and s["draft"]["pick_count"] == 2
    assert [p["name"] for p in s["recent_picks"]] == ["Wideout One", "Rusher One"]
    assert any(sl["player"] and sl["player"]["name"] == "Rusher One" for sl in s["roster"])


def test_keepers_are_pre_made_and_my_altered_slots_are_honoured(draft_store, draft_settings):
    # My round-2 pick (pick 4) is a keeper: it's not "my next pick".
    ctl, loop = _manual(draft_store, draft_settings, keepers=[(T1, 2, "T1")])
    s = loop.poll_once()
    assert s["draft"]["current_pick"] == 1 and s["draft"]["my_next_pick"] == 1
    assert s["draft"]["my_pick_after"] == 5  # pick 4 (keeper) skipped
    assert not any(p["name"] == "Tightend One" for p in s["available"])
    assert any(sl["player"] and sl["player"]["name"] == "Tightend One" for sl in s["roster"])


# -- HTTP end to end ----------------------------------------------------------


def _get(url):
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_http_routes_search_pick_undo_unmark(draft_store, draft_settings):
    ctl, loop = _manual(draft_store, draft_settings)
    loop.poll_once()
    with CompanionServer(loop.snapshot, port=0, manual=ctl) as srv:
        base = srv.url
        state = _get(base + "api/state")
        assert state["manual"] is True
        teams = _get(base + "api/teams")["teams"]
        assert [t["name"] for t in teams] == ["You", "Rival"]
        res = _get(base + "api/search?q=rush%20on")["results"]
        assert res[0]["name"] == "Rusher One" and res[0]["available"]
        status, out = _post(base + "api/pick", {"player": res[0]["raw_id"]})
        assert status == 200 and out["ok"] and out["state"]["available_count"] == 18
        assert out["state"]["manual"] is True
        status, out = _post(base + "api/pick", {"player": "R2", "pick": 1})
        assert status == 400 and "already made" in out["error"]
        status, out = _post(base + "api/undo", {})
        assert status == 200 and out["message"] == "undid pick 1" and out["state"]["available_count"] == 19
        _post(base + "api/pick", {"player": "R2", "team_key": T2})
        status, out = _post(base + "api/unmark", {"pick": 1})
        assert status == 200 and out["state"]["draft"]["current_pick"] == 1
        assert _get(base + "api/search?q=rusher%20two")["results"][0]["available"]


def test_routes_are_absent_without_manual_mode(draft_store, draft_settings):
    ctl, loop = _manual(draft_store, draft_settings)
    loop.poll_once()
    with CompanionServer(loop.snapshot, port=0) as srv:
        assert _get(srv.url + "api/state")["manual"] is False
        status, out = _post(srv.url + "api/pick", {"player": "R1"})
        assert status == 404


# --------------------------------------------------------------------------- #
# Off-board picks (P0-2c): the room drafts a player our store doesn't know
# --------------------------------------------------------------------------- #


def test_off_board_pick_consumes_slot_and_roster_without_stalling(draft_store, draft_settings):
    ctl, loop = _manual(draft_store, draft_settings)
    loop.poll_once()
    out = ctl.mark("R1")  # my pick 1
    # Rival drafts a rookie RB the board has never heard of — pick 2 must be
    # consumable anyway, and their roster must show an RB slot filled.
    out = ctl.mark_unknown("RB", name="Mystery Rookie", team_key=T2)
    s = out["state"]
    assert out["ok"]
    assert s["draft"]["current_pick"] == 3  # no stall: the clock advanced
    assert s["draft"]["on_the_clock"]["is_me"] is False  # snake: Rival again
    rival = next(t for t in s["teams"] if t["team_key"] == T2)
    rb_slots = [sl for sl in rival["roster"] if sl["label"] == "RB" and sl["player"]]
    assert rb_slots and rb_slots[0]["player"]["name"] == "Mystery Rookie"
    assert rb_slots[0]["player"]["position"] == "RB"
    # …and their RB need shrank accordingly (1 of 2 dedicated slots gone).
    assert rival["needs"]["open_starters"].get("RB", 0) == 1
    recent = s["recent_picks"][0]
    assert recent["name"] == "Mystery Rookie" and recent["position"] == "RB"


def test_two_anonymous_off_board_picks_stay_distinct(draft_store, draft_settings):
    ctl, loop = _manual(draft_store, draft_settings)
    loop.poll_once()
    ctl.mark_unknown("LB", team_key=T1)
    out = ctl.mark_unknown("LB", team_key=T2)
    s = out["state"]
    assert s["draft"]["pick_count"] == 2  # not one moved pick
    assert s["draft"]["current_pick"] == 3


def test_off_board_pick_requires_a_position(draft_store, draft_settings):
    ctl, loop = _manual(draft_store, draft_settings)
    loop.poll_once()
    with pytest.raises(ValueError, match="position"):
        ctl.mark_unknown("")


def test_off_board_pick_survives_store_restart(draft_store, draft_settings):
    ctl, loop = _manual(draft_store, draft_settings, record=True)
    loop.poll_once()
    ctl.mark_unknown("DEF", name="Some Defense", team_key=T1)
    # A fresh source + loop restores from draft_picks — the off-board id
    # round-trips through the player_key and still parses.
    src2 = ManualPickSource([T1, T2], rounds=8, game_code="sim")
    restored = src2.restore(draft_store.draft_picks(DRAFT_LEAGUE_KEY))
    assert restored == 1
    clock = FakeClock()
    loop2 = DraftLoop(
        draft_store, draft_settings, src2, my_team_key=T1, mode="manual",
        record_to_store=False, draft_order=[T1, T2],
        time_func=clock.time, sleep_func=clock.sleep,
    )
    s = loop2.poll_once()
    assert s["recent_picks"][0]["name"] == "Some Defense"
    assert s["recent_picks"][0]["position"] == "DEF"


def test_off_board_keeper_recorded_via_position(draft_store, draft_settings):
    from fantasy_coach.draft.manual import KeeperBook
    from fantasy_coach.league import KeeperRules

    src = ManualPickSource([T1, T2], rounds=8, game_code="sim")
    clock = FakeClock()
    loop = DraftLoop(
        draft_store, draft_settings, src, my_team_key=T1, mode="manual",
        record_to_store=False, draft_order=[T1, T2],
        time_func=clock.time, sleep_func=clock.sleep,
    )
    rows = draft_store.sql("SELECT canonical_id, name, position, team FROM players")
    book = KeeperBook(draft_store, DRAFT_LEAGUE_KEY, KeeperRules(), rounds=8)
    ctl = ManualDraft(
        source=src, loop=loop, finder=PlayerFinder(dict(r) for r in rows),
        team_names={T1: "You", T2: "Rival"}, keepers=book,
    )
    loop.poll_once()
    out = ctl.add_keeper(T2, "", last_round=7, off_position="WR", off_name="Rookie Keeper")
    assert out["ok"]
    kept = out["keepers"]["teams"]
    rival = next(t for t in kept if t["team_key"] == T2)
    assert rival["keepers"][0]["name"] == "Rookie Keeper"
    assert rival["keepers"][0]["cost_round"] == 4  # 7 − 3
    # The cost-round pick is pre-made for the keeper.
    pick_no = src.pick_number_for(T2, 4)
    assert pick_no in src.keeper_pick_numbers


def test_http_off_board_route(draft_store, draft_settings):
    ctl, loop = _manual(draft_store, draft_settings)
    loop.poll_once()
    with CompanionServer(loop.snapshot, port=0, manual=ctl) as srv:
        status, out = _post(
            srv.url + "api/pick_offboard",
            {"position": "LB", "name": "Edge Guy", "team_key": T2},
        )
        assert status == 200 and out["ok"]
        assert out["state"]["draft"]["current_pick"] == 2
        status, out = _post(srv.url + "api/pick_offboard", {"position": ""})
        assert status == 400 and "position" in out["error"]
