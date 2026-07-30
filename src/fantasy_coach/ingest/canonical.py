"""The canonical player model (framework §3.1, §3.3).

Every data source in the app — Yahoo, nflverse, Sleeper, projections, Vegas,
weather — is joined onto **one** record per real player, keyed by a single
canonical id. The framework picks **``gsis_id``** (NFL's official player id, the
backbone of nflverse) as the hub, with a synthetic id for the two things that
have no ``gsis_id``: team **defenses** (``DST_{TEAM}``) and players we couldn't
resolve yet (``UNK_{yahoo_id}`` — see the resolver's review queue).

The record separates two concerns the framework is explicit about (§3.3
"attached, not stored on the identity"):

* **Identity** — names, position, team, bye, status, and the crosswalk of every
  provider's native id (:class:`ExternalIds`). Stable within a season.
* **Attached data** — projections, opportunity metrics, game environment,
  market signals, and computed value. These are *slots the value engine (M4)
  fills*; M3 only creates the record and wires the identity + ids. They live in
  small mutable dataclasses so M4 can populate them in place without M3 needing
  to know their internals.

These are plain ``dataclasses`` (the core stays pydantic-free — framework §2.1).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fantasy_coach.ingest.names import clean_name, normalize_position, normalize_team

__all__ = [
    "ExternalIds",
    "Projections",
    "Opportunity",
    "GameEnvironment",
    "MarketSignals",
    "PlayerValue",
    "CanonicalPlayer",
    "dst_canonical_id",
    "unresolved_canonical_id",
]

#: Prefix for synthetic team-defense canonical ids (``DST_SF``).
DST_ID_PREFIX = "DST_"
#: Prefix for synthetic ids of players that failed every resolution stage.
UNRESOLVED_ID_PREFIX = "UNK_"


def dst_canonical_id(team: str) -> str:
    """Synthetic canonical id for a team defense, e.g. ``"DST_SF"`` (§3.2 step 6).

    The team code is normalized first, so Yahoo's ``SF`` and nflverse's ``SFO``
    yield the same defense id.
    """
    return f"{DST_ID_PREFIX}{normalize_team(team) or team.strip().upper()}"


def unresolved_canonical_id(yahoo_player_id: str) -> str:
    """Synthetic id for a player no stage could map (keeps them addressable).

    Rookies and just-signed players lag in the crosswalks (§3.1 "coverage
    reality"); rather than drop them, we mint a stable ``UNK_{yahoo_id}`` so the
    rest of the app can still carry them and the override table can fix them
    later without a schema change.
    """
    return f"{UNRESOLVED_ID_PREFIX}{yahoo_player_id}"


@dataclass(slots=True)
class ExternalIds:
    """Every provider's native id for one player — the spokes of the crosswalk.

    ``gsis_id`` is the hub (§3.1) and normally equals the owning
    :class:`CanonicalPlayer`'s ``canonical_id`` (defenses/unresolved excepted).
    All fields are optional strings: a source that lacks a mapping for a player
    simply leaves its column ``None`` (this is common early-season for Yahoo
    rookies). ``mfl_id`` and ``sleeper_id`` are the framework's strong secondary
    anchors.
    """

    gsis_id: str | None = None
    yahoo_id: str | None = None
    sleeper_id: str | None = None
    espn_id: str | None = None
    pfr_id: str | None = None
    fantasypros_id: str | None = None
    rotowire_id: str | None = None
    sportradar_id: str | None = None
    mfl_id: str | None = None
    pff_id: str | None = None
    cbs_id: str | None = None

    def merge_missing(self, other: "ExternalIds") -> None:
        """Fill any ``None`` id on ``self`` from ``other`` (Sleeper gap-fill, §3.1).

        Existing values are never overwritten — the primary crosswalk
        (DynastyProcess) wins; secondary sources only *fill gaps*.
        """
        for f in ExternalIds.__slots__:  # type: ignore[attr-defined]
            if getattr(self, f) is None:
                val = getattr(other, f)
                if val is not None:
                    setattr(self, f, val)


@dataclass(slots=True)
class Projections:
    """Per-source scoring-adjusted projections + the blended result (M4 §4.1).

    ``by_source`` maps a source name to that source's projected points *in the
    league's scoring* (M4 rescoring). ``blended`` is the weighted consensus M4
    computes; ``floor``/``ceiling`` carry the uncertainty band (§4.1 step 5).
    M3 leaves this empty — it exists so the join target is stable.
    """

    by_source: dict[str, float] = field(default_factory=dict)
    blended: float | None = None
    floor: float | None = None
    ceiling: float | None = None


@dataclass(slots=True)
class Opportunity:
    """Usage / volume metrics from nflverse pbp + snap counts (M4 §4.1 step 3)."""

    snap_pct: float | None = None
    target_share: float | None = None
    air_yards_share: float | None = None
    rz_touches: float | None = None
    routes: float | None = None
    depth_chart_rank: int | None = None


@dataclass(slots=True)
class GameEnvironment:
    """Vegas + weather context for the player's game (M4 §4.1 step 4)."""

    implied_team_total: float | None = None
    spread: float | None = None
    over_under: float | None = None
    weather_wind_mph: float | None = None
    weather_precip_pct: float | None = None
    is_dome: bool = False


@dataclass(slots=True)
class MarketSignals:
    """ADP / rankings / ownership / trend — the market's view (M4 §4.2–4.3)."""

    adp: float | None = None
    adp_stddev: float | None = None
    ecr: float | None = None
    percent_owned: float | None = None
    percent_rostered: float | None = None
    trend_add: int | None = None
    trend_drop: int | None = None


@dataclass(slots=True)
class PlayerValue:
    """Computed value — the value engine's output (M4 §4.2)."""

    vorp: float | None = None
    tier: int | None = None
    scoring_adjusted_points: float | None = None


@dataclass(slots=True)
class CanonicalPlayer:
    """A single player unified across every source (framework §3.3).

    Created by M3's :class:`~fantasy_coach.ingest.index.PlayerIndex` from a Yahoo
    :class:`~fantasy_coach.clients.models.PlayerIdentity` plus the crosswalk. The
    identity block is filled by M3; the ``projections`` / ``opportunity`` /
    ``environment`` / ``market`` / ``value`` slots are filled by M4.

    Attributes:
        canonical_id: The hub id — ``gsis_id`` for real players, ``DST_{TEAM}``
            for defenses, ``UNK_{yahoo_id}`` for unresolved players.
        ids: Every provider's native id (:class:`ExternalIds`).
        name / clean_name: Display name and its normalized form (§3.2).
        position / team: Normalized position and team code.
        bye_week / status: From Yahoo + the nflverse injuries feed.
        is_defense: True for team defenses (mapped by team code, not id).
        resolution_method / resolution_confidence: How the id crosswalk matched
            this player (``override``/``yahoo_id``/``deterministic``/``fuzzy``/
            ``dst``/``unmatched``) and, for fuzzy matches, the score — surfaced
            so a low-confidence join can be reviewed (§3.2 step 4).
    """

    canonical_id: str
    ids: ExternalIds = field(default_factory=ExternalIds)
    name: str = ""
    clean_name: str = ""
    position: str = ""
    team: str = ""
    bye_week: int | None = None
    status: str = ""
    depth_chart_rank: int | None = None
    is_defense: bool = False
    resolution_method: str = ""
    resolution_confidence: float | None = None

    # -- attached data (filled by M4; M3 just provides the slots) -----------
    projections: Projections = field(default_factory=Projections)
    opportunity: Opportunity = field(default_factory=Opportunity)
    environment: GameEnvironment = field(default_factory=GameEnvironment)
    market: MarketSignals = field(default_factory=MarketSignals)
    value: PlayerValue = field(default_factory=PlayerValue)

    @property
    def is_resolved(self) -> bool:
        """True when the id crosswalk mapped this player to a real hub id.

        False for defenses' synthetic ids? No — defenses are *intentionally*
        synthetic and count as resolved. Only ``UNK_`` players are unresolved.
        """
        return not self.canonical_id.startswith(UNRESOLVED_ID_PREFIX)

    @property
    def gsis_id(self) -> str | None:
        """Convenience accessor for the hub id."""
        return self.ids.gsis_id

    @classmethod
    def for_defense(cls, team: str, *, name: str = "", **kwargs: object) -> "CanonicalPlayer":
        """Construct a team-defense record with a synthetic ``DST_{TEAM}`` id."""
        norm_team = normalize_team(team) or team.strip().upper()
        return cls(
            canonical_id=dst_canonical_id(team),
            name=name or f"{norm_team} DST",
            clean_name=clean_name(name) if name else "",
            position="DEF",
            team=norm_team,
            is_defense=True,
            **kwargs,  # type: ignore[arg-type]
        )

    def __post_init__(self) -> None:
        # Keep normalized views coherent even if a caller sets raw values.
        if self.name and not self.clean_name:
            self.clean_name = clean_name(self.name)
        if self.position:
            self.position = normalize_position(self.position)
        if self.team:
            self.team = normalize_team(self.team) or self.team
