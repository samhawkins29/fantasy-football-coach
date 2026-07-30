"""``PlayerIndex`` — the assembled canonical player layer (framework §3, step 4).

This is where M3's pieces come together into the thing M4 consumes: a set of
:class:`~fantasy_coach.ingest.canonical.CanonicalPlayer` records, one per real
player (plus a synthetic record per team defense), each carrying every
provider's id and addressable by *any* of them.

Build flow (framework §3.2 step 4 "Normalize + join everything onto the
canonical players"):

1. Take the league's Yahoo players as
   :class:`~fantasy_coach.clients.models.PlayerIdentity` objects (M2's
   ``Player.identity()``).
2. Resolve each to a canonical id via the
   :class:`~fantasy_coach.ingest.resolver.IdResolver` (§3.2 pipeline).
3. Materialize a :class:`CanonicalPlayer` per resolution, copying the crosswalk
   row's ids onto it and recording *how* it resolved (for audit).
4. Expose lookups by canonical id / Yahoo id / any spoke id, so later joins
   (Sleeper trends, nflverse stats, projections) attach onto the right record.

The index keeps the :class:`ResolutionReport` alongside the players, so a caller
can see coverage and the review queue in one place.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from fantasy_coach.clients.models import PlayerIdentity
from fantasy_coach.ingest.canonical import CanonicalPlayer, ExternalIds
from fantasy_coach.ingest.names import clean_name, normalize_position, normalize_team
from fantasy_coach.ingest.resolver import IdResolver, Resolution, ResolutionReport
from fantasy_coach.ingest.sources import TrendingPlayer

__all__ = ["PlayerIndex", "build_player_index"]


@dataclass(slots=True)
class PlayerIndex:
    """A resolved set of canonical players with multi-id lookup + coverage report.

    Attributes:
        players: ``{canonical_id: CanonicalPlayer}``.
        report: The :class:`ResolutionReport` from building the index.
    """

    players: dict[str, CanonicalPlayer] = field(default_factory=dict)
    report: ResolutionReport = field(default_factory=ResolutionReport)
    # Secondary indices: provider id -> canonical id.
    _by_yahoo: dict[str, str] = field(default_factory=dict, repr=False)
    _by_sleeper: dict[str, str] = field(default_factory=dict, repr=False)
    _by_gsis: dict[str, str] = field(default_factory=dict, repr=False)

    # -- construction --------------------------------------------------------

    def add(self, player: CanonicalPlayer) -> None:
        """Insert / replace a canonical player and refresh the id indices.

        If a player with the same ``canonical_id`` exists (e.g. two Yahoo rows
        for one defense, or a re-add), their ids are merged rather than clobbered.
        """
        existing = self.players.get(player.canonical_id)
        if existing is not None:
            existing.ids.merge_missing(player.ids)
            player = existing
        else:
            self.players[player.canonical_id] = player
        self._reindex(player)

    def _reindex(self, player: CanonicalPlayer) -> None:
        """(Re)point the secondary id indices at ``player``'s canonical id."""
        cid = player.canonical_id
        if player.ids.yahoo_id:
            self._by_yahoo[player.ids.yahoo_id] = cid
        if player.ids.sleeper_id:
            self._by_sleeper[player.ids.sleeper_id] = cid
        if player.ids.gsis_id:
            self._by_gsis[player.ids.gsis_id] = cid

    # -- lookups -------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.players)

    def __contains__(self, canonical_id: object) -> bool:
        return canonical_id in self.players

    def get(self, canonical_id: str) -> CanonicalPlayer | None:
        """Look up by canonical id."""
        return self.players.get(canonical_id)

    def by_yahoo_id(self, yahoo_id: str) -> CanonicalPlayer | None:
        """Look up by Yahoo player id."""
        cid = self._by_yahoo.get(yahoo_id)
        return self.players.get(cid) if cid else None

    def by_sleeper_id(self, sleeper_id: str) -> CanonicalPlayer | None:
        """Look up by Sleeper id."""
        cid = self._by_sleeper.get(sleeper_id)
        return self.players.get(cid) if cid else None

    def by_gsis_id(self, gsis_id: str) -> CanonicalPlayer | None:
        """Look up by the hub ``gsis_id``."""
        cid = self._by_gsis.get(gsis_id)
        return self.players.get(cid) if cid else None

    @property
    def defenses(self) -> list[CanonicalPlayer]:
        """Every team-defense record in the index."""
        return [p for p in self.players.values() if p.is_defense]

    @property
    def unresolved(self) -> list[CanonicalPlayer]:
        """Players carried with a synthetic ``UNK_`` id (the review list)."""
        return [p for p in self.players.values() if not p.is_resolved]

    # -- joins (framework §3.2 step 4) --------------------------------------

    def attach_sleeper_trending(self, trending: Iterable[TrendingPlayer]) -> int:
        """Join Sleeper trending add/drop counts onto players; return #joined.

        Routes each trending row's ``sleeper_id`` through the index and writes
        ``market.trend_add`` / ``market.trend_drop`` (the waiver signal M7 reads).
        """
        joined = 0
        for row in trending:
            player = self.by_sleeper_id(row.sleeper_id)
            if player is None:
                continue
            if row.direction == "add":
                player.market.trend_add = row.count
            elif row.direction == "drop":
                player.market.trend_drop = row.count
            joined += 1
        return joined

    def attach_yahoo_market(self, identities_market: Mapping[str, Mapping[str, float]]) -> int:
        """Join Yahoo ADP / ownership (by Yahoo id) onto ``market``; return #joined.

        ``identities_market`` maps ``yahoo_id -> {"adp": .., "percent_owned": ..}``
        — e.g. built from ``Player.average_draft_pick`` / ``percent_owned`` after
        a ``get_players(..., out=("draft_analysis", "percent_owned"))`` pull.
        """
        joined = 0
        for yid, m in identities_market.items():
            player = self.by_yahoo_id(yid)
            if player is None:
                continue
            if "adp" in m and m["adp"] is not None:
                player.market.adp = float(m["adp"])
            if "percent_owned" in m and m["percent_owned"] is not None:
                player.market.percent_owned = float(m["percent_owned"])
            joined += 1
        return joined


def _canonical_from_identity(identity: PlayerIdentity, resolution: Resolution) -> CanonicalPlayer:
    """Build a :class:`CanonicalPlayer` from a Yahoo identity + its resolution.

    Ids come from the matched crosswalk row when there is one; the Yahoo id is
    always stamped on (even for unmatched players) so the record stays joinable
    from the Yahoo side. Identity fields (name/pos/team/bye/status) come from
    Yahoo — the source of truth for *this* league's view of the player.
    """
    row = resolution.row
    ids = ExternalIds(yahoo_id=identity.yahoo_player_id or None)
    if row is not None:
        for field_name, value in row.ids.items():
            setattr(ids, field_name, value)
    # Ensure the Yahoo id from the identity is never lost to a row that lacked it.
    if identity.yahoo_player_id:
        ids.yahoo_id = identity.yahoo_player_id
    # For a resolved real (non-defense) player, the canonical id *is* the gsis
    # hub (that is how the resolver builds it), so stamp it on if the matched
    # row didn't already carry one — e.g. an override to a gsis not in the map.
    if resolution.matched and not identity.is_defense and ids.gsis_id is None:
        ids.gsis_id = resolution.canonical_id

    player = CanonicalPlayer(
        canonical_id=resolution.canonical_id,
        ids=ids,
        name=identity.full_name,
        clean_name=clean_name(identity.full_name),
        position=normalize_position(identity.position),
        team=normalize_team(identity.team_abbr) or identity.team_abbr,
        bye_week=identity.bye_week,
        status=identity.status,
        is_defense=identity.is_defense,
        resolution_method=resolution.method,
        resolution_confidence=resolution.confidence,
    )
    return player


def build_player_index(
    identities: Iterable[PlayerIdentity],
    resolver: IdResolver,
) -> PlayerIndex:
    """Resolve Yahoo identities and assemble a :class:`PlayerIndex` (§3.2 step 4).

    This is the M3 entry point M4 calls: hand it the league's players (as
    :class:`PlayerIdentity`) and a configured :class:`IdResolver`, get back a
    fully-indexed canonical player set plus the coverage/review report.

    Args:
        identities: The league's Yahoo players (``[p.identity() for p in players]``).
        resolver: A configured :class:`IdResolver` (crosswalk + overrides).

    Returns:
        A :class:`PlayerIndex` whose ``report`` carries match-rate, per-method
        counts, the fuzzy review queue, and the unmatched list.
    """
    index = PlayerIndex()
    for identity in identities:
        resolution = resolver.resolve(identity)
        index.report.add(resolution)
        index.add(_canonical_from_identity(identity, resolution))
    return index
