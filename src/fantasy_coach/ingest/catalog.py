"""The full player universe from Sleeper's free catalog (P0-2 / P0-4).

The nflverse projection model can only see players with NFL history — rookies,
team defenses, and this year's depth-chart churn are invisible to it, so a
store warmed offline from projections alone has **no rookies and no DEFs** at
all. Sleeper's ``GET /players/nfl`` (free, keyless — the same endpoint the
status source already uses) carries the entire current player universe with
cross-platform ids (``gsis_id``/``yahoo_id``), positions, teams, and depth
charts. This module turns that blob into :class:`CanonicalPlayer` rows the
store can **merge** over its existing table:

* a player the store already knows (gsis-keyed) gains a ``sleeper_id``, a
  ``yahoo_id`` (what live Yahoo picks resolve through!), and a current team;
* a rookie or DEF the store has never seen becomes a new row — which is what
  lets the FFC ADP feed (:mod:`fantasy_coach.ingest.adp`) resolve them and
  the board's ADP gap-fill price them.

Canonical ids follow the existing hub convention: ``gsis_id`` when Sleeper
carries one (leading whitespace stripped — the blob is messy), ``DST_{TEAM}``
for team defenses, and ``SLP_{sleeper_id}`` for players Sleeper hasn't mapped
to gsis yet (early-career rookies).

Cache/offline posture matches every other source: :meth:`warm_cache` pulls and
persists a compact JSON cache; :meth:`load` serves it with zero network.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from fantasy_coach.ingest.canonical import CanonicalPlayer, ExternalIds
from fantasy_coach.ingest.names import normalize_position, normalize_team
from fantasy_coach.ingest.sources import SleeperSource

__all__ = [
    "CATALOG_POSITIONS",
    "SLEEPER_CATALOG_METHOD",
    "SleeperCatalogSource",
    "catalog_players",
]

logger = logging.getLogger(__name__)

#: Normalized positions worth carrying into the players table — every position
#: any startable slot could want, kickers included (config decides
#: startability, not the catalog).
CATALOG_POSITIONS = frozenset({"QB", "RB", "WR", "TE", "K", "DEF", "DL", "LB", "DB"})

#: ``resolution_method`` stamped on rows sourced from the Sleeper catalog —
#: distinguishable from crosswalk methods and projection synthesis in the DB.
SLEEPER_CATALOG_METHOD = "sleeper_catalog"


def _canonical_id_for(pid: str, obj: Mapping[str, object], position: str, team: str) -> str:
    """The hub id: gsis > DST_{team} (defenses) > SLP_{sleeper_id}."""
    gsis = str(obj.get("gsis_id") or "").strip()
    if gsis:
        return gsis
    if position == "DEF":
        return f"DST_{team or pid}"
    return f"SLP_{pid}"


def catalog_players(blob: Mapping[str, Mapping[str, object]]) -> list[CanonicalPlayer]:
    """Convert Sleeper's ``players/nfl`` blob into canonical player rows.

    Keeps active, rostered players at :data:`CATALOG_POSITIONS` (plus every
    team DEF). Free agents and retired players are dropped — they are not
    draftable inventory and would bloat the finder.
    """
    out: list[CanonicalPlayer] = []
    for pid, obj in blob.items():
        if not isinstance(obj, Mapping):
            continue
        position = normalize_position(str(obj.get("position") or ""))
        if position not in CATALOG_POSITIONS:
            continue
        team = normalize_team(str(obj.get("team") or ""))
        if not obj.get("active") or not team:
            continue  # unrostered / retired — not draftable inventory
        is_def = position == "DEF"
        if is_def:
            name = f"{obj.get('first_name', '')} {obj.get('last_name', '')}".strip()
            name = name or f"{team} Defense"
        else:
            name = f"{obj.get('first_name', '')} {obj.get('last_name', '')}".strip()
        if not name:
            continue
        gsis = str(obj.get("gsis_id") or "").strip()
        yahoo = obj.get("yahoo_id")
        depth = obj.get("depth_chart_order")
        out.append(
            CanonicalPlayer(
                canonical_id=_canonical_id_for(str(pid), obj, position, team),
                ids=ExternalIds(
                    gsis_id=gsis or None,
                    sleeper_id=str(pid),
                    yahoo_id=str(yahoo) if yahoo not in (None, "") else None,
                ),
                name=name,
                position=position,
                team=team,
                depth_chart_rank=int(depth) if isinstance(depth, int) else None,
                is_defense=is_def,
                resolution_method=SLEEPER_CATALOG_METHOD,
            )
        )
    return out


@dataclass(slots=True)
class SleeperCatalogSource:
    """Cache-first access to the converted Sleeper player catalog.

    Args:
        sleeper: The fetch layer (injectable — tests pass a
            :class:`SleeperSource` wired to ``httpx.MockTransport``).
        cache_dir: Where the converted-catalog JSON cache lives (git-ignored).
    """

    name: str = "sleeper_catalog"
    sleeper: SleeperSource = field(default_factory=SleeperSource)
    cache_dir: Path = field(default_factory=lambda: Path(".cache"))

    @property
    def is_live(self) -> bool:
        """True — Sleeper needs no credentials."""
        return True

    def _cache_path(self) -> Path:
        return self.cache_dir / "sleeper_catalog.json"

    def warm_cache(self) -> list[CanonicalPlayer]:
        """Pull the live blob, convert, persist the compact cache, return rows."""
        players = catalog_players(self.sleeper.players())
        if not players:
            raise RuntimeError("Sleeper catalog pull returned no usable players")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "source": self.name,
            "players": [
                {
                    "canonical_id": p.canonical_id,
                    "gsis_id": p.ids.gsis_id,
                    "sleeper_id": p.ids.sleeper_id,
                    "yahoo_id": p.ids.yahoo_id,
                    "name": p.name,
                    "position": p.position,
                    "team": p.team,
                    "depth": p.depth_chart_rank,
                    "is_defense": p.is_defense,
                }
                for p in players
            ],
        }
        self._cache_path().write_text(json.dumps(payload), encoding="utf-8")
        return players

    def load(self) -> list[CanonicalPlayer]:
        """The cached catalog with zero network. Raises if never warmed."""
        path = self._cache_path()
        if not path.exists():
            raise RuntimeError(
                f"no Sleeper catalog cache at {path} — run warm_cache() while online"
            )
        payload = json.loads(path.read_text(encoding="utf-8"))
        return [
            CanonicalPlayer(
                canonical_id=str(r["canonical_id"]),
                ids=ExternalIds(
                    gsis_id=r.get("gsis_id") or None,
                    sleeper_id=r.get("sleeper_id") or None,
                    yahoo_id=r.get("yahoo_id") or None,
                ),
                name=str(r.get("name", "")),
                position=str(r.get("position", "")),
                team=str(r.get("team", "")),
                depth_chart_rank=r.get("depth"),
                is_defense=bool(r.get("is_defense")),
                resolution_method=SLEEPER_CATALOG_METHOD,
            )
            for r in payload.get("players", [])
        ]
