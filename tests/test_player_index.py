"""Tests for PlayerIndex assembly + joins (M3, framework §3 step 4)."""

from __future__ import annotations

from fantasy_coach.ingest.index import build_player_index
from fantasy_coach.ingest.resolver import IdResolver
from fantasy_coach.ingest.sources import TrendingPlayer

from .conftest import make_identity


def _identities():
    return [
        make_identity(yahoo_player_id="30123", full_name="Patrick Mahomes", team_abbr="KC", position="QB", bye_week=6),
        make_identity(yahoo_player_id="34123", full_name="Ken Walker III", team_abbr="SEA", position="RB", bye_week=5),
        make_identity(yahoo_player_id="100014", full_name="San Francisco", team_abbr="SF", position="DEF"),
        make_identity(yahoo_player_id="45001", full_name="Freshman Rookie", team_abbr="NYJ", position="WR"),
    ]


def test_build_index_creates_canonical_players(crosswalk):
    index = build_player_index(_identities(), IdResolver(crosswalk))
    assert len(index) == 4

    mahomes = index.by_yahoo_id("30123")
    assert mahomes is not None
    assert mahomes.canonical_id == "00-0033873"
    assert mahomes.gsis_id == "00-0033873"
    assert mahomes.ids.sleeper_id == "4046"  # copied from the crosswalk row
    assert mahomes.bye_week == 6
    assert mahomes.resolution_method == "yahoo_id"


def test_index_carries_defense_and_unresolved(crosswalk):
    index = build_player_index(_identities(), IdResolver(crosswalk))
    assert index.get("DST_SF").is_defense
    assert [p.canonical_id for p in index.defenses] == ["DST_SF"]
    unresolved = index.unresolved
    assert len(unresolved) == 1
    assert unresolved[0].canonical_id == "UNK_45001"
    assert unresolved[0].ids.yahoo_id == "45001"  # still joinable from Yahoo side


def test_index_multi_id_lookup(crosswalk):
    index = build_player_index(_identities(), IdResolver(crosswalk))
    mahomes = index.by_gsis_id("00-0033873")
    assert mahomes is index.by_sleeper_id("4046")
    assert mahomes is index.by_yahoo_id("30123")


def test_report_available_on_index(crosswalk):
    index = build_player_index(_identities(), IdResolver(crosswalk))
    summary = index.report.summary()
    assert summary["total"] == 4
    assert summary["by_method"]["dst"] == 1
    assert summary["unmatched"] == 1


def test_attach_sleeper_trending(crosswalk):
    index = build_player_index(_identities(), IdResolver(crosswalk))
    # Ken Walker sleeper_id is 8151 in the crosswalk row.
    trending = [
        TrendingPlayer(sleeper_id="8151", count=28422, direction="add"),
        TrendingPlayer(sleeper_id="does-not-exist", count=5, direction="add"),
    ]
    joined = index.attach_sleeper_trending(trending)
    assert joined == 1
    walker = index.by_yahoo_id("34123")
    assert walker.market.trend_add == 28422


def test_attach_yahoo_market(crosswalk):
    index = build_player_index(_identities(), IdResolver(crosswalk))
    joined = index.attach_yahoo_market({"30123": {"adp": 12.4, "percent_owned": 99.9}})
    assert joined == 1
    assert index.by_yahoo_id("30123").market.adp == 12.4
    assert index.by_yahoo_id("30123").market.percent_owned == 99.9


def test_gap_fill_then_resolve_end_to_end(crosswalk):
    # Ken Walker has no yahoo_id in DynastyProcess. After Sleeper gap-fill adds
    # yahoo_id 34123, a direct id join resolves him (not just the deterministic
    # fallback) — the full §3.1/§3.2 happy path.
    crosswalk.gap_fill_from_sleeper({"8151": {"yahoo_id": 34123}})
    resolver = IdResolver(crosswalk)
    ident = make_identity(yahoo_player_id="34123", full_name="K. Walker", team_abbr="SEA", position="RB")
    res = resolver.resolve(ident)
    assert res.method == "yahoo_id"
    assert res.canonical_id == "00-0037746"
