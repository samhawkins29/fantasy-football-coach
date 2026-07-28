"""Typed models for the Yahoo Fantasy read client (M2).

These are plain ``dataclasses`` (no ``pydantic`` — the framework §2.1 keeps the
core pydantic-free because of the Windows Rust-core DLL issue). Every model is a
*clean* view over Yahoo's notoriously nested JSON: the parsers in
:mod:`fantasy_coach.clients.parsers` flatten Yahoo's positional / count-indexed
structures into these flat records.

Two cross-cutting conventions:

* **``raw`` passthrough.** Each top-level model keeps the flattened Yahoo dict it
  was built from in a ``raw`` field, so debugging and one-off field access never
  require re-fetching. It is excluded from ``repr`` to keep output readable.
* **Player identity for M3.** :class:`Player` (and the player-ish records inside
  draft picks / transactions) captures Yahoo's ``player_id`` **and**
  ``player_key`` plus name / team / position, which is exactly what M3's
  id-mapping (§3, hub id = ``gsis_id``) crosswalks against. No source is joined
  on name in the hot path, but the raw name/team/pos are retained so the
  deterministic and fuzzy fallbacks (§3.2) have something to match on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

# A flattened Yahoo object (single-level dict of primitives / small structures).
RawDict = dict[str, object]

#: Roster slots that are not starting positions. Yahoo spells the injury slot
#: several ways depending on how old the league is.
BENCH_POSITIONS = frozenset({"BN", "IR", "IL", "IR+", "IL+", "NA"})

#: Yahoo stat ids worth naming — these are the ones that define a format.
STAT_ID_RECEPTIONS = 11  # PPR value lives here
STAT_ID_PASSING_TDS = 5  # 4-pt vs 6-pt passing TD leagues


# --------------------------------------------------------------------------- #
# Game / league discovery
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Game:
    """A Yahoo fantasy "game" — one sport for one season.

    The ``game_key`` (e.g. ``"449"``) is the season-specific prefix for every
    league / team / player key. Discovered via ``/game/nfl`` (framework §2.3).
    """

    game_key: str
    game_id: str = ""
    name: str = ""
    code: str = ""
    season: str = ""
    is_current: bool = False
    raw: RawDict = field(default_factory=dict, repr=False)


@dataclass(slots=True)
class League:
    """A fantasy league the authenticated user belongs to.

    ``league_key`` has the shape ``{game_key}.l.{league_id}`` (e.g.
    ``449.l.123456``) and is the handle every other read method takes.
    """

    league_key: str
    league_id: str = ""
    name: str = ""
    season: str = ""
    game_key: str = ""
    num_teams: int | None = None
    scoring_type: str = ""
    league_type: str = ""
    draft_status: str = ""
    current_week: int | None = None
    start_week: int | None = None
    end_week: int | None = None
    url: str = ""
    is_finished: bool = False
    raw: RawDict = field(default_factory=dict, repr=False)


# --------------------------------------------------------------------------- #
# League settings + scoring
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class RosterPosition:
    """One roster slot definition — e.g. 2×RB, 1×W/R/T flex, 1×DEF.

    ``position`` is Yahoo's slot code (``QB``/``RB``/``WR``/``TE``/``W/R/T``/
    ``Q/W/R/T`` superflex/``K``/``DEF``/``BN`` bench/``IR``). ``count`` is how
    many of that slot the league starts. M4's VORP baselines are derived from
    these counts (framework §4.2, §3.4).
    """

    position: str
    count: int = 0
    position_type: str = ""
    is_starting_position: bool = True

    @property
    def is_flex(self) -> bool:
        """True for multi-position slots (``W/R/T``, ``Q/W/R/T``)."""
        return "/" in self.position

    @property
    def flex_positions(self) -> list[str]:
        """The positions this slot accepts (``["W", "R", "T"]`` for a flex)."""
        return self.position.split("/") if self.is_flex else [self.position]


@dataclass(slots=True)
class StatCategory:
    """A scored stat plus its per-unit point value.

    ``value`` is the point modifier from the league's ``stat_modifiers`` (e.g.
    ``0.04`` per passing yard, ``6.0`` per rushing TD, ``1.0`` per reception in
    full-PPR). ``None`` means the stat is tracked but not directly scored.
    M4 rescoring (§4.1) multiplies each source's stat line by these values.
    """

    stat_id: int
    name: str = ""
    display_name: str = ""
    position_type: str = ""
    value: float | None = None
    sort_order: str = ""


@dataclass(slots=True)
class LeagueSettings:
    """Scoring + roster + waiver/playoff configuration for a league.

    This is the raw material for M3's ``ScoringProfile`` (framework §3.4): the
    ``stat_categories`` carry the point-per-stat map, ``roster_positions`` the
    slot counts, and the waiver/FAAB/playoff flags the rest of the rules.
    """

    league_key: str = ""
    scoring_type: str = ""
    draft_type: str = ""
    is_auction_draft: bool = False
    uses_playoff: bool = False
    playoff_start_week: int | None = None
    num_playoff_teams: int | None = None
    num_playoff_consolation_teams: int | None = None
    uses_playoff_reseeding: bool = False
    uses_faab: bool = False
    waiver_type: str = ""
    waiver_rule: str = ""
    waiver_time: int | None = None
    trade_end_date: str = ""
    trade_ratify_type: str = ""
    trade_reject_time: int | None = None
    uses_negative_points: bool = False
    max_teams: int | None = None
    roster_positions: list[RosterPosition] = field(default_factory=list)
    stat_categories: list[StatCategory] = field(default_factory=list)
    raw: RawDict = field(default_factory=dict, repr=False)

    def stat_modifiers(self) -> dict[int, float]:
        """Return ``{stat_id: point_value}`` for every directly-scored stat.

        Convenience view for M4 rescoring — skips categories with no modifier.
        """
        return {
            cat.stat_id: cat.value
            for cat in self.stat_categories
            if cat.value is not None
        }

    def points_for(self, stat_id: int) -> float:
        """Points per unit of ``stat_id`` — ``0.0`` when the stat isn't scored."""
        for cat in self.stat_categories:
            if cat.stat_id == stat_id and cat.value is not None:
                return cat.value
        return 0.0

    def starting_slots(self) -> dict[str, int]:
        """Return ``{position: count}`` for starting slots only (excludes BN/IR)."""
        return {
            pos.position: pos.count
            for pos in self.roster_positions
            if pos.is_starting_position
        }

    def flex_slots(self) -> list[RosterPosition]:
        """Return every multi-position (flex) starting slot."""
        return [
            pos
            for pos in self.roster_positions
            if pos.is_starting_position and pos.is_flex
        ]

    @property
    def bench_slots(self) -> int:
        """Number of bench (``BN``) spots."""
        return sum(p.count for p in self.roster_positions if p.position == "BN")

    @property
    def injury_slots(self) -> int:
        """Number of IR/IL spots."""
        return sum(
            p.count
            for p in self.roster_positions
            if p.position in BENCH_POSITIONS and p.position != "BN"
        )

    @property
    def roster_size(self) -> int:
        """Total roster spots — starters + bench + IR."""
        return sum(pos.count for pos in self.roster_positions)

    @property
    def is_superflex(self) -> bool:
        """True when a flex slot accepts a QB (``Q/W/R/T``).

        Superflex moves QB replacement level enormously, so M4's VORP baselines
        (§4.2 "dynamic baselines") must branch on this.
        """
        return any("Q" in slot.flex_positions for slot in self.flex_slots())

    @property
    def points_per_reception(self) -> float:
        """PPR value: ``0`` standard, ``0.5`` half-PPR, ``1.0`` full PPR."""
        return self.points_for(STAT_ID_RECEPTIONS)

    @property
    def ppr_type(self) -> str:
        """``"standard"`` / ``"half_ppr"`` / ``"ppr"`` / ``"custom_ppr"``."""
        ppr = self.points_per_reception
        if ppr == 0:
            return "standard"
        if ppr == 0.5:
            return "half_ppr"
        if ppr == 1.0:
            return "ppr"
        return "custom_ppr"


# --------------------------------------------------------------------------- #
# Teams + managers + rosters
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Manager:
    """A team's manager (owner)."""

    manager_id: str = ""
    nickname: str = ""
    guid: str = ""
    is_current_login: bool = False


@dataclass(slots=True)
class Team:
    """A team within a league (there are ``num_teams`` of these).

    ``is_owned_by_current_login`` marks *your* team — the one the coach manages.
    ``faab_balance`` / ``waiver_priority`` feed M7's waiver engine.
    """

    team_key: str
    team_id: str = ""
    name: str = ""
    url: str = ""
    is_owned_by_current_login: bool = False
    waiver_priority: int | None = None
    faab_balance: int | None = None
    number_of_moves: int | None = None
    number_of_trades: int | None = None
    managers: list[Manager] = field(default_factory=list)
    raw: RawDict = field(default_factory=dict, repr=False)


@dataclass(slots=True)
class PlayerRank:
    """One of Yahoo's own ranking values for a player.

    ``rank_type`` is Yahoo's code — ``PR`` preseason rank, ``PS`` projected
    season rank, ``AR`` actual rank, ``OR`` overall rank. These are a *free*
    market signal that M4 blends alongside external ranks (§4.1).
    """

    rank_type: str = ""
    rank_value: int | None = None
    rank_season: str = ""


@dataclass(slots=True)
class DraftAnalysis:
    """Yahoo's own ADP/AAV aggregates for a player.

    A free ADP source, and the input M5's draft-survival model (§4.3) needs:
    ``average_pick`` is the μ of the ADP distribution, ``percent_drafted`` how
    reliably the player goes at all.
    """

    average_pick: float | None = None
    average_round: float | None = None
    average_cost: float | None = None
    percent_drafted: float | None = None


@dataclass(frozen=True, slots=True)
class PlayerIdentity:
    """The minimum a player needs to enter M3's id crosswalk (§3.2).

    M3 joins Yahoo → the canonical ``gsis_id`` primarily on
    :attr:`yahoo_player_id` (the ``yahoo_id`` column of the DynastyProcess /
    Sleeper id maps), and falls back to a normalized ``(name, position, team)``
    tuple when that mapping is missing — which it will be for 2026 rookies
    (§3.1 "coverage reality"). Everything both paths need is captured here, so
    M3 never re-parses a Yahoo payload.
    """

    yahoo_player_id: str
    yahoo_player_key: str
    full_name: str
    first_name: str
    last_name: str
    team_abbr: str
    position: str
    eligible_positions: tuple[str, ...] = ()
    position_type: str = ""
    bye_week: int | None = None
    status: str = ""

    @property
    def is_defense(self) -> bool:
        """True for team defenses, which §3.2 maps by team code, not player id."""
        return self.position == "DEF" or self.position_type == "DT"


@dataclass(slots=True)
class Player:
    """A player in the league's universe (or on a roster).

    Captures both Yahoo id forms (``player_id`` and ``player_key``) plus
    name/team/position so M3 (§3.2) can crosswalk to the canonical ``gsis_id``.
    ``eligible_positions`` drives the start/sit optimizer's slot assignment (M6).

    ``selected_position`` is only populated when the player is read *from a
    roster* (it is the slot they're currently started in for that week);
    ``percent_owned`` only when the ``percent_owned`` sub-resource was requested.
    """

    player_key: str
    player_id: str = ""
    full_name: str = ""
    first_name: str = ""
    last_name: str = ""
    ascii_full_name: str = ""
    editorial_team_abbr: str = ""
    editorial_team_full_name: str = ""
    display_position: str = ""
    position_type: str = ""
    primary_position: str = ""
    eligible_positions: list[str] = field(default_factory=list)
    uniform_number: str = ""
    bye_week: int | None = None
    status: str = ""
    status_full: str = ""
    injury_note: str = ""
    on_disabled_list: bool = False
    percent_owned: float | None = None
    percent_owned_delta: float | None = None
    is_undroppable: bool = False
    image_url: str = ""
    url: str = ""
    selected_position: str = ""
    selected_position_is_flex: bool = False
    player_ranks: list[PlayerRank] = field(default_factory=list)
    draft_analysis: DraftAnalysis | None = None
    points: float | None = None
    raw: RawDict = field(default_factory=dict, repr=False)

    @property
    def position(self) -> str:
        """Best available position string (``primary_position`` preferred)."""
        return self.primary_position or self.display_position

    @property
    def average_draft_pick(self) -> float | None:
        """Yahoo ADP as a bare float, or ``None`` if not requested."""
        return self.draft_analysis.average_pick if self.draft_analysis else None

    def rank(self, rank_type: str) -> int | None:
        """Return the rank value for a Yahoo rank type (``"PR"``, ``"AR"``...)."""
        for entry in self.player_ranks:
            if entry.rank_type == rank_type:
                return entry.rank_value
        return None

    def identity(self) -> PlayerIdentity:
        """Project down to the fields M3's id crosswalk needs (§3.2)."""
        return PlayerIdentity(
            yahoo_player_id=self.player_id,
            yahoo_player_key=self.player_key,
            full_name=self.full_name,
            first_name=self.first_name,
            last_name=self.last_name,
            team_abbr=self.editorial_team_abbr,
            position=self.position,
            eligible_positions=tuple(self.eligible_positions),
            position_type=self.position_type,
            bye_week=self.bye_week,
            status=self.status,
        )


@dataclass(slots=True)
class RosterSlot:
    """A player as they sit on a team's roster for a given week.

    ``selected_position`` is the slot the player occupies (a starting slot, or
    ``BN``/``IR``). ``is_starting`` is a convenience flag (not bench/IR/empty).
    """

    player: Player
    selected_position: str = ""
    is_editable: bool = False

    @property
    def is_starting(self) -> bool:
        """True when the player occupies a starting slot (not bench/IR/empty)."""
        return bool(self.selected_position) and (
            self.selected_position not in BENCH_POSITIONS
        )


@dataclass(slots=True)
class TeamRoster:
    """A team's full roster for one week — the start/sit state M6 optimizes.

    ``week`` is the coverage week Yahoo returned (which may differ from the one
    requested if the league has not reached it yet).
    """

    team_key: str = ""
    week: int | None = None
    coverage_type: str = ""
    is_editable: bool = False
    slots: list[RosterSlot] = field(default_factory=list)
    raw: RawDict = field(default_factory=dict, repr=False)

    @property
    def players(self) -> list[Player]:
        """Every rostered player, starters and bench alike."""
        return [slot.player for slot in self.slots]

    @property
    def starters(self) -> list[RosterSlot]:
        """Slots currently in a starting position (not BN/IR)."""
        return [slot for slot in self.slots if slot.is_starting]

    @property
    def bench(self) -> list[RosterSlot]:
        """Slots on the bench or an injury slot."""
        return [slot for slot in self.slots if not slot.is_starting]


# --------------------------------------------------------------------------- #
# Draft results
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class DraftPick:
    """One pick from ``draftresults`` — the live-draft polling target (M5).

    ``cost`` is only populated in auction drafts. ``player_key`` is what M5's
    survival model marks as "gone" as the board fills in real time (§4.3).
    """

    pick: int
    round: int
    team_key: str = ""
    player_key: str = ""
    cost: int | None = None
    raw: RawDict = field(default_factory=dict, repr=False)

    @property
    def is_made(self) -> bool:
        """True once the pick has actually happened.

        During a live draft Yahoo returns the *whole* pick list up front, with
        empty ``player_key``/``team_key`` for picks not yet made. M5's poller
        must diff on this rather than on list length (§7 "diff, don't re-pull").
        """
        return bool(self.player_key)

    @property
    def player_id(self) -> str:
        """The bare Yahoo player id from :attr:`player_key`, for M3 joins."""
        return self.player_key.rsplit(".", 1)[-1] if self.player_key else ""


# --------------------------------------------------------------------------- #
# Transactions
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class TransactionPlayer:
    """A player moved by a transaction, with its source/destination.

    ``movement_type`` is ``add``/``drop``. ``source_type`` /
    ``destination_type`` are ``waivers``/``freeagents``/``team`` — the signal
    M7/M9 read to see who was picked up or dropped.
    """

    player_key: str = ""
    player_id: str = ""
    name: str = ""
    editorial_team_abbr: str = ""
    display_position: str = ""
    movement_type: str = ""
    source_type: str = ""
    source_team_key: str = ""
    destination_type: str = ""
    destination_team_key: str = ""


@dataclass(slots=True)
class Transaction:
    """An add/drop/trade/commish move from ``transactions``.

    ``faab_bid`` is populated for FAAB waiver claims (framework §4.4/§7 — a
    waiver signal). ``players`` lists everyone the transaction moved.
    """

    transaction_key: str
    transaction_id: str = ""
    type: str = ""
    status: str = ""
    timestamp: int | None = None
    faab_bid: int | None = None
    trader_team_key: str = ""
    tradee_team_key: str = ""
    players: list[TransactionPlayer] = field(default_factory=list)
    raw: RawDict = field(default_factory=dict, repr=False)

    @property
    def datetime_utc(self) -> datetime | None:
        """The transaction time as an aware UTC datetime."""
        if self.timestamp is None:
            return None
        return datetime.fromtimestamp(self.timestamp, tz=timezone.utc)

    @property
    def added(self) -> list[TransactionPlayer]:
        """Legs where a player joined a roster."""
        return [leg for leg in self.players if leg.movement_type == "add"]

    @property
    def dropped(self) -> list[TransactionPlayer]:
        """Legs where a player left a roster."""
        return [leg for leg in self.players if leg.movement_type == "drop"]


# --------------------------------------------------------------------------- #
# Matchups / scoreboard
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class MatchupTeam:
    """One side of a matchup, with actual + projected points."""

    team_key: str = ""
    name: str = ""
    points: float | None = None
    projected_points: float | None = None


@dataclass(slots=True)
class Matchup:
    """A weekly head-to-head matchup from the scoreboard.

    ``winner_team_key`` is set once the matchup is final. M11's calibration loop
    (§4.5) joins projected vs actual points off these.
    """

    week: int | None = None
    week_start: str = ""
    week_end: str = ""
    status: str = ""
    is_playoffs: bool = False
    is_consolation: bool = False
    is_tied: bool = False
    winner_team_key: str = ""
    teams: list[MatchupTeam] = field(default_factory=list)
    raw: RawDict = field(default_factory=dict, repr=False)

    @property
    def team_keys(self) -> list[str]:
        """The two team keys involved, in Yahoo's order."""
        return [side.team_key for side in self.teams]

    def opponent_of(self, team_key: str) -> MatchupTeam | None:
        """Return the other side of this matchup, or ``None`` if not involved."""
        if team_key not in self.team_keys:
            return None
        for side in self.teams:
            if side.team_key != team_key:
                return side
        return None


__all__ = [
    "RawDict",
    "BENCH_POSITIONS",
    "STAT_ID_RECEPTIONS",
    "STAT_ID_PASSING_TDS",
    "Game",
    "League",
    "RosterPosition",
    "StatCategory",
    "LeagueSettings",
    "Manager",
    "Team",
    "PlayerRank",
    "DraftAnalysis",
    "PlayerIdentity",
    "Player",
    "RosterSlot",
    "TeamRoster",
    "DraftPick",
    "TransactionPlayer",
    "Transaction",
    "MatchupTeam",
    "Matchup",
]
