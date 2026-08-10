"""Step 6 — injury status + durability risk, fully offline.

Covers the whole chain: status normalization and the Yahoo↔Sleeper merge
precedence, the durability model (monotonic discount, risk thresholds, cache
round-trip), the playoff-risk interaction, board integration (weight 0 =
bit-identical ranking), store persistence + vintage refresh, and the live
loop's status re-check dropping a newly-Out player mid-draft.
"""

from __future__ import annotations

import pytest

from fantasy_coach.ingest.injury import (
    DURABILITY_DISCOUNT_CAP,
    DurabilityProfile,
    DurabilitySource,
    InjuryReport,
    RISK_ELEVATED,
    RISK_HIGH,
    RISK_LOW,
    RISK_MODERATE,
    SleeperStatusSource,
    durability_discount,
    merge_reports,
    normalize_status,
    risk_flag,
    sleeper_injuries,
)
from fantasy_coach.ingest.sources import NflverseSource
from fantasy_coach.value.injury import (
    STATUS_DISCOUNTS,
    TOTAL_DISCOUNT_CAP,
    PlayerRisk,
    build_risk_index,
    injury_multiplier,
    injury_note,
    total_discount,
)
from tests.conftest import DRAFT_LEAGUE_KEY, FakeClock, make_draft_pool

# --------------------------------------------------------------------------- #
# Status normalization
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Yahoo spellings
        ("Q", "Q"),
        ("D", "D"),
        ("O", "O"),
        ("IR", "IR"),
        ("IR-R", "IR"),
        ("PUP-P", "PUP"),
        ("NFI-R", "NFI"),
        ("SUSP", "SUS"),
        ("NA", "NA"),
        # Sleeper spellings
        ("Questionable", "Q"),
        ("Doubtful", "D"),
        ("Out", "O"),
        ("Sus", "SUS"),
        ("COV", "O"),
        ("DNR", "NA"),
        ("PUP", "PUP"),
        # healthy / unknown never invent a designation
        ("", ""),
        (None, ""),
        ("Active", ""),
        ("P", ""),
        ("Probable", ""),
        ("garbled-code", ""),
    ],
)
def test_normalize_status(raw, expected):
    assert normalize_status(raw) == expected


def test_status_discount_direction():
    # The founder's ordering requirement: Out << Questionable << Healthy.
    assert STATUS_DISCOUNTS[""] == 0.0
    assert 0 < STATUS_DISCOUNTS["Q"] < STATUS_DISCOUNTS["D"]
    assert STATUS_DISCOUNTS["D"] < STATUS_DISCOUNTS["O"] < STATUS_DISCOUNTS["IR"]


# --------------------------------------------------------------------------- #
# Merge precedence (Yahoo + Sleeper)
# --------------------------------------------------------------------------- #

T0 = "2026-08-10T12:00:00+00:00"
T0_PLUS_5MIN = "2026-08-10T12:05:00+00:00"
T0_PLUS_8H = "2026-08-10T20:00:00+00:00"


def test_merge_same_vintage_most_severe_wins():
    yahoo = InjuryReport(source="yahoo", status="Q", fetched_at=T0)
    sleeper = InjuryReport(source="sleeper", status="O", fetched_at=T0_PLUS_5MIN)
    winner = merge_reports([yahoo, sleeper])
    assert winner.status == "O"
    assert winner.source == "sleeper"  # source label survives the merge


def test_merge_severity_tie_breaks_to_most_recent():
    older = InjuryReport(source="yahoo", status="Q", fetched_at=T0)
    newer = InjuryReport(source="sleeper", status="Q", fetched_at=T0_PLUS_5MIN)
    assert merge_reports([older, newer]).source == "sleeper"


def test_merge_meaningfully_fresher_report_wins_even_if_milder():
    # A player cleared this morning must escape last night's OUT.
    stale_out = InjuryReport(source="yahoo", status="O", fetched_at=T0)
    fresh_ok = InjuryReport(source="sleeper", status="", fetched_at=T0_PLUS_8H)
    winner = merge_reports([stale_out, fresh_ok])
    assert winner.status == ""
    assert winner.source == "sleeper"


def test_merge_empty_and_single():
    assert merge_reports([]) is None
    only = InjuryReport(source="yahoo", status="D", fetched_at=T0)
    assert merge_reports([only]) is only


# --------------------------------------------------------------------------- #
# Sleeper blob parsing + resolution
# --------------------------------------------------------------------------- #

SLEEPER_BLOB = {
    "1001": {  # designated, resolvable by sleeper id
        "team": "KC",
        "full_name": "Rusher One",
        "position": "RB",
        "injury_status": "Out",
        "injury_body_part": "Knee",
        "practice_participation": "DNP",
        "yahoo_id": 201,
    },
    "1002": {  # healthy but on a roster — must still emit (it clears flags)
        "team": "SF",
        "full_name": "Wideout One",
        "position": "WR",
        "injury_status": None,
        "gsis_id": "W1",
    },
    "1003": {"team": None, "full_name": "Retired Guy", "position": "RB"},  # skipped
}


def test_sleeper_injuries_parses_blob():
    rows = sleeper_injuries(SLEEPER_BLOB, fetched_at=T0)
    by_id = {r.sleeper_id: r for r in rows}
    assert set(by_id) == {"1001", "1002"}  # team-less row dropped
    hurt = by_id["1001"]
    assert hurt.report.status == "O"
    assert hurt.report.raw_status == "Out"
    assert hurt.report.detail == "Knee"
    assert hurt.report.practice == "DNP"
    assert hurt.report.fetched_at == T0
    assert hurt.yahoo_id == "201"
    assert by_id["1002"].report.status == ""  # healthy is data, not absence


class _FakeSleeper:
    def players(self):
        return SLEEPER_BLOB


def test_sleeper_status_source_resolves_through_id_spokes():
    source = SleeperStatusSource(
        _FakeSleeper(),
        by_sleeper_id={"1001": "R1"},
        by_gsis_id={"W1": "W1"},
        by_yahoo_id={},
        now=lambda: T0,
    )
    fetched = source.fetch()
    assert fetched["R1"].status == "O"  # via sleeper id
    assert fetched["W1"].status == ""  # via gsis fallback
    assert len(fetched) == 2  # unresolvable rows dropped


# --------------------------------------------------------------------------- #
# Durability: discount monotonicity, thresholds, source + cache
# --------------------------------------------------------------------------- #


def test_durability_discount_monotonic_and_clamped():
    values = [durability_discount(m) for m in (0.0, 1.0, 2.5, 4.0, 6.0, 10.0, 17.0)]
    assert values == sorted(values)  # more missed football never reads safer
    assert values[0] == 0.0
    assert values[-1] == DURABILITY_DISCOUNT_CAP  # clamped: a nudge, not a swing
    # soft-tissue bump is bounded and monotonic too
    assert durability_discount(1.0, 1) > durability_discount(1.0, 0)
    assert durability_discount(1.0, 99) == durability_discount(1.0, 3)


@pytest.mark.parametrize(
    "avg_missed,expected",
    [
        (0.0, RISK_LOW),
        (1.49, RISK_LOW),
        (1.5, RISK_MODERATE),
        (3.49, RISK_MODERATE),
        (3.5, RISK_HIGH),
        (5.99, RISK_HIGH),
        (6.0, RISK_ELEVATED),
        (12.0, RISK_ELEVATED),
    ],
)
def test_risk_flag_thresholds(avg_missed, expected):
    assert risk_flag(avg_missed) == expected


def _weekly_row(gsis, year, week, pos="RB", name="Somebody"):
    return {
        "player_id": gsis,
        "season": year,
        "week": week,
        "season_type": "REG",
        "position": pos,
        "player_display_name": name,
    }


#: IRONMAN plays every week of every season; GLASS plays 5 games a season.
DURABILITY_WEEKLY = (
    [_weekly_row("IRONMAN", y, w, name="Iron Man") for y in (2023, 2024, 2025) for w in range(1, 18)]
    + [_weekly_row("GLASS", y, w, name="Glass Guy") for y in (2024, 2025) for w in range(1, 6)]
)

DURABILITY_INJURIES = [
    {"gsis_id": "GLASS", "season": 2025, "report_status": "Out", "report_primary_injury": "Hamstring"},
    {"gsis_id": "GLASS", "season": 2025, "report_status": "Out", "report_primary_injury": "Hamstring"},
    {"gsis_id": "GLASS", "season": 2024, "report_status": "Doubtful", "report_primary_injury": "Groin"},
    {"gsis_id": "IRONMAN", "season": 2025, "report_status": "Questionable", "report_primary_injury": "Ankle"},
]


def _durability_source(tmp_path, injuries=DURABILITY_INJURIES):
    def injuries_fetch(years):
        if injuries is None:
            raise RuntimeError("injuries feed down")
        return injuries

    return DurabilitySource(
        nflverse=NflverseSource(
            fetchers={"weekly": lambda years: DURABILITY_WEEKLY, "injuries": injuries_fetch}
        ),
        cache_dir=tmp_path,
    )


def test_durability_source_profiles(tmp_path):
    profiles = {p.canonical_id: p for p in _durability_source(tmp_path).warm_cache(2026)}
    iron, glass = profiles["IRONMAN"], profiles["GLASS"]

    assert iron.avg_missed == 0.0
    assert iron.risk == RISK_LOW
    assert iron.discount == 0.0
    assert iron.soft_tissue == 0  # ankle is not soft tissue

    assert glass.avg_missed == 12.0  # missed 12 every season played
    assert glass.risk == RISK_ELEVATED
    assert glass.discount == DURABILITY_DISCOUNT_CAP
    assert glass.games == {2024: 5.0, 2025: 5.0}
    assert glass.designations == 3
    assert glass.soft_tissue == 2  # (2025, hamstring) deduped; + (2024, groin)
    assert glass.total_missed == 24.0
    assert "not a probability" in glass.note  # the honesty label rides along


def test_durability_source_cache_roundtrip_and_feed_degradation(tmp_path):
    _durability_source(tmp_path).warm_cache(2026)
    # load() must serve the cache without touching the fetchers at all
    reloaded = DurabilitySource(
        nflverse=NflverseSource(fetchers={}), cache_dir=tmp_path
    ).load(2026)
    assert {p.canonical_id for p in reloaded} == {"IRONMAN", "GLASS"}
    assert {p.canonical_id: p.discount for p in reloaded}["GLASS"] == DURABILITY_DISCOUNT_CAP

    # a failing injuries feed degrades to games-missed-only, never an error
    profiles = _durability_source(tmp_path, injuries=None).warm_cache(2027)
    glass = {p.canonical_id: p for p in profiles}["GLASS"]
    assert glass.designations == 0 and glass.soft_tissue == 0
    assert glass.risk == RISK_ELEVATED  # games missed still speaks


# --------------------------------------------------------------------------- #
# Valuation: combined discount, playoff interaction, notes
# --------------------------------------------------------------------------- #


def _risk(status="", avg_missed=0.0, soft=0):
    report = InjuryReport(source="yahoo", status=status) if status else None
    profile = None
    if avg_missed:
        profile = DurabilityProfile(
            canonical_id="X",
            avg_missed=avg_missed,
            seasons_seen=2,
            games={2024: 17 - avg_missed, 2025: 17 - avg_missed},
            soft_tissue=soft,
            risk=risk_flag(avg_missed),
            discount=durability_discount(avg_missed, soft),
        )
    return PlayerRisk(report=report, durability=profile)


def test_playoff_weight_amplifies_durability_but_not_status():
    durable_risk = _risk(avg_missed=4.0)
    assert total_discount(durable_risk, playoff_weight=0.3) > total_discount(
        durable_risk, playoff_weight=0.0
    )
    status_risk = _risk(status="Q")
    assert total_discount(status_risk, playoff_weight=0.3) == total_discount(
        status_risk, playoff_weight=0.0
    )


def test_total_discount_capped_and_multiplier_off_at_zero_weight():
    worst = _risk(status="IR", avg_missed=17.0, soft=3)
    assert total_discount(worst, playoff_weight=1.0) == TOTAL_DISCOUNT_CAP
    assert injury_multiplier(worst, weight=0.0) == 1.0  # the off-by-default contract
    assert injury_multiplier(worst, weight=1.0) == 1.0 - TOTAL_DISCOUNT_CAP


def test_injury_note_narrates_status_and_risk():
    r = _risk(status="Q", avg_missed=6.0)
    r.report.detail = "Knee"
    note = injury_note(r, shaded_pct=8.0)
    assert "QUESTIONABLE (knee) — monitor [yahoo]" in note
    assert "elevated re-injury risk — missed 12 games over 2 seasons" in note
    assert "value shaded 8%" in note
    assert injury_note(PlayerRisk()) == ""  # healthy, no history → silence


# --------------------------------------------------------------------------- #
# Board integration (uses the hand-computable conftest pool)
# --------------------------------------------------------------------------- #


def _build_board(risk=None, injury_weight=0.0):
    from fantasy_coach.clients.models import LeagueSettings, RosterPosition
    from fantasy_coach.value.board import build_value_board

    settings = LeagueSettings(
        league_key=DRAFT_LEAGUE_KEY,
        max_teams=2,
        roster_positions=[
            RosterPosition(position="QB", count=1),
            RosterPosition(position="RB", count=2),
            RosterPosition(position="WR", count=2),
            RosterPosition(position="TE", count=1),
            RosterPosition(position="W/R/T", count=1),
        ],
    )
    players, projections = make_draft_pool()
    return build_value_board(
        projections, settings, players=players, risk=risk, injury_weight=injury_weight
    )


def test_board_weight_zero_is_bit_identical_with_flags_visible():
    base = _build_board()
    risk = build_risk_index(
        {"R1": InjuryReport(source="yahoo", status="O", detail="Knee")},
        {"W1": _risk(avg_missed=6.0).durability},
    )
    flagged = _build_board(risk=risk, injury_weight=0.0)

    assert [e.canonical_id for e in base.entries] == [
        e.canonical_id for e in flagged.entries
    ]
    assert [(e.vorp, e.draft_value) for e in base.entries] == [
        (e.vorp, e.draft_value) for e in flagged.entries
    ]
    by_id = {e.canonical_id: e for e in flagged.entries}
    assert by_id["R1"].injury_status == "O"  # flag visible…
    assert by_id["R1"].injury_discount is None  # …value untouched
    assert by_id["W1"].durability_risk == RISK_ELEVATED
    assert flagged.injury_weight == 0.0


def test_board_weight_shades_value_and_reorders():
    # R1 (VORP 100) ruled OUT at full weight: 100 × (1−0.30) = 70 < R2's 80.
    risk = build_risk_index({"R1": InjuryReport(source="sleeper", status="O")})
    board = _build_board(risk=risk, injury_weight=1.0)
    by_id = {e.canonical_id: e for e in board.entries}
    assert by_id["R1"].vorp == 100.0  # raw VORP never touched
    assert by_id["R1"].draft_value == 70.0
    assert by_id["R1"].injury_discount == 0.30
    assert "OUT" in by_id["R1"].injury_note and "value shaded 30%" in by_id["R1"].injury_note
    assert by_id["R2"].overall_rank < by_id["R1"].overall_rank  # dropped below R2
    # tiers cluster season VORP — identical to the unshaded board
    base_tiers = {e.canonical_id: e.tier for e in _build_board().entries}
    assert {e.canonical_id: e.tier for e in board.entries} == base_tiers


# --------------------------------------------------------------------------- #
# Store persistence + vintage refresh
# --------------------------------------------------------------------------- #


def test_store_reports_roundtrip_merge_and_vintage_refresh(tmp_path):
    from fantasy_coach.store import CoachStore

    clock = {"now": "2026-08-10T00:00:00+00:00"}
    store = CoachStore(":memory:", now=lambda: clock["now"])

    store.upsert_injury_reports(
        {"R1": InjuryReport(source="yahoo", status="Q", fetched_at=T0)}, source="yahoo"
    )
    store.upsert_injury_reports(
        {"R1": InjuryReport(source="sleeper", status="O", fetched_at=T0_PLUS_5MIN)},
        source="sleeper",
    )
    merged = store.injury_reports()
    assert merged["R1"].status == "O" and merged["R1"].source == "sleeper"
    assert store.injury_reports(source="yahoo")["R1"].status == "Q"
    by_source = store.injury_reports_by_source()
    assert set(by_source) == {"yahoo", "sleeper"}

    vintages = {r["scope"]: r["refreshed_at"] for r in store.vintage()}
    assert vintages["injury_status:sleeper"] == "2026-08-10T00:00:00+00:00"

    # a refresh (re-pull) moves the vintage stamp — the founder's staleness check
    clock["now"] = "2026-08-11T09:00:00+00:00"
    store.upsert_injury_reports(
        {"R1": InjuryReport(source="sleeper", status="", fetched_at=T0_PLUS_8H)},
        source="sleeper",
    )
    vintages = {r["scope"]: r["refreshed_at"] for r in store.vintage()}
    assert vintages["injury_status:sleeper"] == "2026-08-11T09:00:00+00:00"
    store.close()


def test_store_durability_roundtrip(tmp_path):
    from fantasy_coach.store import CoachStore

    store = CoachStore(":memory:")
    profile = _risk(avg_missed=4.0, soft=1).durability
    store.upsert_durability([profile])
    loaded = store.durability_profiles()["X"]
    assert loaded.avg_missed == 4.0
    assert loaded.risk == RISK_HIGH
    assert loaded.discount == profile.discount
    assert loaded.games == profile.games
    assert {r["scope"] for r in store.vintage()} >= {"durability"}
    store.close()


def test_replace_board_persists_injury_columns(draft_store, draft_settings):
    from fantasy_coach.value.board import build_value_board

    players, projections = make_draft_pool()
    risk = build_risk_index({"R1": InjuryReport(source="yahoo", status="IR")})
    board = build_value_board(
        projections, draft_settings, players=players, risk=risk, injury_weight=1.0
    )
    draft_store.replace_board(DRAFT_LEAGUE_KEY, board)
    row = draft_store.sql(
        "SELECT * FROM value_board WHERE league_key = ? AND canonical_id = 'R1'",
        [DRAFT_LEAGUE_KEY],
    )[0]
    assert row["injury_status"] == "IR"
    assert row["injury_discount"] == 0.5
    assert "IR" in row["injury_note"]
    meta = draft_store.board_meta(DRAFT_LEAGUE_KEY)
    assert meta["injury_weight"] == 1.0


# --------------------------------------------------------------------------- #
# warm_store: yahoo status synthesis + durability persistence
# --------------------------------------------------------------------------- #


def test_warm_store_injury_flow(draft_settings):
    from fantasy_coach.store import CoachStore, warm_store

    store = CoachStore(":memory:")
    players, projections = make_draft_pool()
    players[4].status = "IR"  # R1 (index 4 in DRAFT_POOL) — a Yahoo designation
    profile = _risk(avg_missed=6.0).durability
    profile.canonical_id = "W1"
    result = warm_store(
        store,
        draft_settings,
        projections=projections,
        players=players,
        durability=[profile],
        injury_weight=1.0,
        season=2026,
    )
    assert "injury_status:yahoo" in result.refreshed
    assert "durability" in result.refreshed

    reports = store.injury_reports()
    assert reports["R1"].status == "IR" and reports["R1"].source == "yahoo"

    row = store.sql(
        "SELECT * FROM value_board WHERE canonical_id = 'R1'"
    )[0]
    assert row["injury_status"] == "IR"
    assert row["draft_value"] == pytest.approx(50.0)  # 100 × (1 − 0.5)
    w1 = store.sql("SELECT * FROM value_board WHERE canonical_id = 'W1'")[0]
    assert w1["durability_risk"] == RISK_ELEVATED
    store.close()


# --------------------------------------------------------------------------- #
# Live loop: status re-check drops a newly-Out player
# --------------------------------------------------------------------------- #


class FlippingStatusSource:
    """Healthy on the first fetch; R1 ruled OUT from the second onward."""

    name = "sleeper"

    def __init__(self):
        self.fetches = 0

    def fetch(self):
        self.fetches += 1
        if self.fetches == 1:
            return {"R1": InjuryReport(source="sleeper", status="", fetched_at=T0)}
        return {
            "R1": InjuryReport(
                source="sleeper", status="O", detail="Knee", fetched_at=T0_PLUS_5MIN
            )
        }


class EmptyPickSource:
    def fetch(self):
        return []


def _status_loop(draft_store, draft_settings, *, injury_weight, status_source=None, status_interval=0.0):
    from fantasy_coach.draft.loop import DraftLoop

    clock = FakeClock()
    loop = DraftLoop(
        draft_store,
        draft_settings,
        EmptyPickSource(),
        my_team_key=f"{DRAFT_LEAGUE_KEY}.t.1",
        mode="simulation",
        status_source=status_source,
        status_interval=status_interval,
        injury_weight=injury_weight,
        time_func=clock.time,
        sleep_func=clock.sleep,
    )
    return loop, clock


def test_loop_status_recheck_drops_newly_out_player(draft_store, draft_settings):
    source = FlippingStatusSource()
    loop, _ = _status_loop(
        draft_store, draft_settings, injury_weight=1.0, status_source=source
    )
    loop.poll_once()
    assert loop.recommendation.player.entry.canonical_id == "R1"

    # No pick changed — only the status did. The re-check must still recompute.
    snap = loop.poll_once()
    assert source.fetches == 2
    rec = loop.recommendation.player.entry
    assert rec.canonical_id == "R2"  # R1: 100 × 0.7 = 70 < R2's 80
    r1 = next(p for p in snap["available"] if p["canonical_id"] == "R1")
    assert r1["injury_status"] == "O"
    assert "OUT (knee) [sleeper]" in r1["injury_note"]
    assert snap["injury"]["weight"] == 1.0
    assert snap["injury"]["status_source"] == "sleeper"
    assert snap["injury"]["flagged_count"] == 1
    # the live re-check mirrors into the store, moving its vintage
    assert draft_store.injury_reports()["R1"].status == "O"
    assert any(
        row["scope"] == "injury_status:sleeper" for row in draft_store.vintage()
    )


def test_loop_weight_zero_flags_without_reordering(draft_store, draft_settings):
    source = FlippingStatusSource()
    loop, _ = _status_loop(
        draft_store, draft_settings, injury_weight=0.0, status_source=source
    )
    loop.poll_once()
    snap = loop.poll_once()
    assert loop.recommendation.player.entry.canonical_id == "R1"  # ranking unchanged
    r1 = next(p for p in snap["available"] if p["canonical_id"] == "R1")
    assert r1["injury_status"] == "O"  # …but the flag is visible
    assert any("OUT" in reason for reason in snap["recommendation"]["reasons"])


def test_loop_status_interval_paces_the_source(draft_store, draft_settings):
    source = FlippingStatusSource()
    loop, clock = _status_loop(
        draft_store,
        draft_settings,
        injury_weight=1.0,
        status_source=source,
        status_interval=120.0,
    )
    loop.poll_once()
    loop.poll_once()  # same fake second — inside the interval
    assert source.fetches == 1
    clock.advance(121)
    loop.poll_once()
    assert source.fetches == 2


def test_loop_snapshot_carries_vintage(draft_store, draft_settings):
    loop, _ = _status_loop(draft_store, draft_settings, injury_weight=0.0)
    snap = loop.poll_once()
    scopes = {v["scope"] for v in snap["vintage"]}
    assert any(s.startswith("projections") for s in scopes)
    assert any(s.startswith("value_board") for s in scopes)
