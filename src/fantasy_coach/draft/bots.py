"""Realistic mock-draft opponents (upgrade 4).

The naive scripted opponent (best ADP under a few positional caps) produces
drafts no human room produces: nobody reaches, nobody panics into a run,
nobody hoards a position or grabs their RB1's handcuff. Since the simulator
is both the dress rehearsal for draft night *and* the environment the
survival model (upgrade 2) is judged against, its opponents need to draft
the way rooms actually flow.

Each opponent is a :class:`BotProfile` — a small set of tendencies dialled
per team (a room is a mix of archetypes: strict market drafters, value
hunters, need-fillers, reachers, RB-heavy, zero-RB, QB-early, handcuffers).
Every pick, a bot scores each of the top :data:`CANDIDATE_POOL` available
players by market order in **"picks" units** (bigger = wants more):

* **market**: ``−ADP`` (board rank as pseudo-ADP for unpriced players) —
  the anchor everyone shares;
* **value**: ``−overall board rank``, mixed in by ``value_weight`` (value
  hunters draft the board, market bots draft the room's consensus);
* **need**: roster construction — an open dedicated starting slot at the
  position is worth ``+NEED_STARTER_PICKS × need_weight``, an open flex a
  bit less, and positions whose starters are all set get nothing (bench is
  value only). Uses the *same* roster-slot machinery the founder's own
  recommendation uses (:mod:`fantasy_coach.draft.recommend`);
* **run**: the room's measured positional-run intensity
  (:func:`~fantasy_coach.draft.survival.room_state` — the same detector the
  survival model reads) × ``run_sensitivity`` — panic drafters join runs;
* **handcuff**: an RB on the same NFL team as one of the bot's own top RBs,
  once the draft is in its back half, gets ``+HANDCUFF_PICKS × handcuff``;
* **bye**: a player whose bye is already shared by two-plus of the bot's
  starters loses ``BYE_PICKS × bye_aware`` per extra collision;
* **archetype bias**: a flat per-position nudge (RB-heavy, zero-RB, QB-early,
  TE-early…), plus an "elite premium" that reaches for a top-3 QB/TE when
  the profile says so;
* **noise**: Gaussian jitter whose spread grows with the pick number
  (``reach × NOISE_SLOPE × pick``) — pick 1 is nearly deterministic, pick 90
  is not, exactly the shape of real ADP spread — from a seeded RNG so every
  scripted draft is reproducible.

Hard rules no archetype breaks: positional caps (no third QB in a 1-QB
league, one K/DEF), no K/DEF before the last :data:`KICKER_ROUNDS_LEFT`
rounds, and a starting lineup must be fillable — a bot with an open starting
slot and only that many picks left takes a starter.

Determinism: ``BotRoom(seed=…)`` seeds one :class:`random.Random`; the same
seed, board, and settings always produce the same draft (tests pin it).
"""

from __future__ import annotations

import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from fantasy_coach.clients.models import LeagueSettings
from fantasy_coach.draft.recommend import (
    NEED_FLEX,
    NEED_STARTER,
    RosterNeeds,
    assign_roster,
    compute_needs,
    roster_slots,
)
from fantasy_coach.draft.survival import room_state

__all__ = [
    "BotProfile",
    "BotRoom",
    "ARCHETYPES",
    "CANDIDATE_POOL",
    "KICKER_ROUNDS_LEFT",
    "profiles_for_room",
]

#: How many of the top available (by market order) a bot considers each pick.
CANDIDATE_POOL = 14
#: Utility weights, all in "picks" units.
NEED_STARTER_PICKS = 6.0
NEED_FLEX_PICKS = 3.0
RUN_PICKS = 4.0
HANDCUFF_PICKS = 10.0
BYE_PICKS = 3.0
ELITE_PICKS = 8.0
#: Noise spread per pick number at ``reach=1`` (σ = reach × slope × pick).
NOISE_SLOPE = 0.12
#: K/DEF never before this many of the bot's rounds remain.
KICKER_ROUNDS_LEFT = 2
#: Individual defensive players (IDP-lite leagues) drafted in the back stretch
#: — each bot draws its own threshold from this range so the room doesn't
#: take ten IDPs in the same round.
IDP_ROUNDS_LEFT = (3, 8)
#: At most this many IDPs per team in total (one-slot IDP leagues).
IDP_TOTAL_CAP = 2
IDP_POSITIONS = ("DL", "LB", "DB")
#: A backup QB (1-QB leagues) only in the last rounds; a second TE only in
#: the very last ones (rooms rarely roster two). Each bot draws its own
#: threshold from these ranges so the whole room doesn't flip on one round.
BACKUP_QB_ROUNDS_LEFT = (2, 5)
BACKUP_TE_ROUNDS_LEFT = (1, 3)
#: A backup QB/TE is bench-only value: shaded by this many picks so RB/WR
#: depth wins the near-ties (a bot still takes a clearly-mispriced one).
BACKUP_PICKS = 4.0
#: Handcuff logic switches on once this share of the draft is done.
HANDCUFF_AFTER_FRACTION = 0.5
#: Position caps per team (RB/WR are bench depth — uncapped).
POSITION_CAPS: dict[str, int] = {"QB": 2, "TE": 2, "K": 1, "DEF": 1, "DL": 2, "LB": 2, "DB": 2}
SUPERFLEX_QB_CAP = 3
#: "Elite" = this positional rank or better (for the QB/TE-early premium) and
#: "my RB1/RB2 worth handcuffing" = this RB rank or better.
ELITE_POS_RANK = 3
HANDCUFF_RB_RANK = 24


@dataclass(slots=True)
class BotProfile:
    """One opponent's tendencies (see the module docstring for each term)."""

    name: str
    reach: float = 0.6
    value_weight: float = 0.3
    need_weight: float = 1.0
    run_sensitivity: float = 0.5
    handcuff: float = 0.3
    bye_aware: float = 0.5
    elite_qb: float = 0.0
    elite_te: float = 0.0
    pos_bias: dict[str, float] = field(default_factory=dict)


#: The archetype mix a room is dealt from (rotated by team slot).
ARCHETYPES: tuple[BotProfile, ...] = (
    BotProfile("market", reach=0.4, value_weight=0.1, need_weight=0.8, run_sensitivity=0.4),
    BotProfile("value hunter", reach=0.3, value_weight=0.9, need_weight=0.7, run_sensitivity=0.1),
    BotProfile("need filler", reach=0.6, value_weight=0.3, need_weight=1.6, run_sensitivity=0.6),
    BotProfile("reacher", reach=1.4, value_weight=0.2, need_weight=0.9, run_sensitivity=1.0),
    BotProfile("rb heavy", reach=0.6, value_weight=0.3, pos_bias={"RB": 3.0, "WR": -1.0}),
    BotProfile("zero rb", reach=0.6, value_weight=0.3, pos_bias={"WR": 3.0, "RB": -3.0, "TE": 1.0}),
    BotProfile("qb early", reach=0.7, value_weight=0.3, elite_qb=1.0, pos_bias={"QB": 1.5}),
    BotProfile("handcuffer", reach=0.5, value_weight=0.4, handcuff=1.0, bye_aware=1.0),
    BotProfile("te early", reach=0.7, value_weight=0.3, elite_te=1.0),
    BotProfile("panic drafter", reach=1.0, value_weight=0.2, run_sensitivity=1.4, need_weight=1.2),
)


def profiles_for_room(num_teams: int, *, offset: int = 0) -> list[BotProfile]:
    """One profile per team slot, cycling through :data:`ARCHETYPES`."""
    return [
        ARCHETYPES[(i + offset) % len(ARCHETYPES)] for i in range(num_teams)
    ]


@dataclass(slots=True)
class _Team:
    key: str
    profile: BotProfile
    picks: list[Mapping[str, object]] = field(default_factory=list)
    backup_qb_rounds: int = 3
    backup_te_rounds: int = 2
    idp_rounds: int = 5

    def pos_count(self, pos: str) -> int:
        return sum(1 for p in self.picks if p.get("position") == pos)


class BotRoom:
    """A room of profiled opponents that draft from a stored board.

    Args:
        board_rows: The board rows to draft from — mappings with
            ``canonical_id``, ``name``, ``position``, ``team``, ``adp``,
            ``overall_rank``, ``pos_rank``, ``bye_week``. Order irrelevant.
        settings: The league (roster construction drives needs + caps).
        team_keys: The teams in round-1 order.
        rounds: Draft length.
        seed: RNG seed (deterministic drafts).
        profiles: One :class:`BotProfile` per team key; default cycles the
            archetypes.
    """

    def __init__(
        self,
        board_rows: Sequence[Mapping[str, object]],
        settings: LeagueSettings,
        team_keys: Sequence[str],
        *,
        rounds: int,
        seed: int = 0,
        profiles: Sequence[BotProfile] | None = None,
    ) -> None:
        self._rows = {str(r["canonical_id"]): r for r in board_rows}
        self._settings = settings
        self._rounds = rounds
        self._rng = random.Random(f"{seed}:{settings.league_key}:{len(team_keys)}")
        profs = list(profiles) if profiles is not None else profiles_for_room(len(team_keys))
        self._teams = {
            key: _Team(
                key=key,
                profile=profs[i % len(profs)],
                backup_qb_rounds=self._rng.randint(*BACKUP_QB_ROUNDS_LEFT),
                backup_te_rounds=self._rng.randint(*BACKUP_TE_ROUNDS_LEFT),
                idp_rounds=self._rng.randint(*IDP_ROUNDS_LEFT),
            )
            for i, key in enumerate(team_keys)
        }
        self._taken: set[str] = set()
        self._made: list[tuple[int, str, float | None]] = []
        self._qb_cap = SUPERFLEX_QB_CAP if settings.is_superflex else POSITION_CAPS["QB"]
        # Market order once: ADP, then board rank after every priced player.
        self._market = sorted(
            self._rows.values(),
            key=lambda r: (
                r["adp"] if r.get("adp") is not None else 10_000 + int(r["overall_rank"]),
                int(r["overall_rank"]),
            ),
        )

    # -- public ----------------------------------------------------------------

    def profile_of(self, team_key: str) -> BotProfile:
        return self._teams[team_key].profile

    def roster_of(self, team_key: str) -> list[Mapping[str, object]]:
        return list(self._teams[team_key].picks)

    def pick(self, team_key: str, pick_no: int, round_no: int) -> Mapping[str, object]:
        """Choose (and record) this team's pick. Returns the board row."""
        team = self._teams[team_key]
        rounds_left = self._rounds - round_no + 1
        needs = self._needs(team)
        chosen = self._choose(team, pick_no, round_no, rounds_left, needs)
        self._record(team, pick_no, chosen)
        return chosen

    def reserve(self, canonical_id: str) -> None:
        """Take a player off the board without assigning him yet (a keeper
        whose cost-round pick comes later — nobody else may draft him)."""
        self._taken.add(canonical_id)

    def record_external(self, team_key: str, pick_no: int, canonical_id: str) -> None:
        """Record a pick made outside the room (e.g. the founder's real pick)."""
        row = self._rows.get(canonical_id)
        if row is None:
            return
        self._record(self._teams[team_key], pick_no, row)

    # -- internals -------------------------------------------------------------

    def _record(self, team: _Team, pick_no: int, row: Mapping[str, object]) -> None:
        cid = str(row["canonical_id"])
        self._taken.add(cid)
        team.picks.append(row)
        adp = row.get("adp")
        self._made.append((pick_no, str(row.get("position", "")), None if adp is None else float(adp)))

    def _needs(self, team: _Team) -> RosterNeeds:
        return compute_needs(assign_roster(roster_slots(self._settings), team.picks))

    def _available(self) -> list[Mapping[str, object]]:
        return [r for r in self._market if str(r["canonical_id"]) not in self._taken]

    def _allowed(self, team: _Team, row: Mapping[str, object], rounds_left: int, needs: RosterNeeds) -> bool:
        pos = str(row.get("position", ""))
        cap = self._qb_cap if pos == "QB" else POSITION_CAPS.get(pos)
        have = team.pos_count(pos)
        if cap is not None and have >= cap:
            return False
        if pos in ("K", "DEF") and rounds_left > KICKER_ROUNDS_LEFT:
            return False
        if pos in IDP_POSITIONS:
            if sum(team.pos_count(p) for p in IDP_POSITIONS) >= IDP_TOTAL_CAP:
                return False
            if rounds_left > team.idp_rounds:
                return False
        if pos in ("QB", "TE") and have >= 1 and not self._settings.is_superflex:
            limit = team.backup_qb_rounds if pos == "QB" else team.backup_te_rounds
            if needs.tag(pos) != NEED_STARTER and rounds_left > limit:
                return False  # no backup QB/TE hoarding mid-draft
        # Must-fill: with exactly as many picks left as (fillable) open
        # starting slots, every pick has to be a starter.
        fillable = self._fillable_positions()
        open_starters = sum(
            n for p, n in needs.open_starters.items() if p in fillable
        ) + sum(1 for elig in needs.open_flex if any(p in fillable for p in elig))
        if open_starters >= rounds_left and needs.tag(pos) not in (NEED_STARTER, NEED_FLEX):
            return False
        return True

    def _fillable_positions(self) -> set[str]:
        """Positions with at least one player still on the board."""
        return {str(r.get("position", "")) for r in self._available()}

    def _choose(
        self, team: _Team, pick_no: int, round_no: int, rounds_left: int, needs: RosterNeeds
    ) -> Mapping[str, object]:
        prof = team.profile
        available = self._available()
        allowed = [r for r in available if self._allowed(team, r, rounds_left, needs)]
        candidates = allowed[:CANDIDATE_POOL]
        # Every open starting slot's best available player is always on the
        # table — otherwise a late K/DEF (deep in market order) could never
        # be drafted and the must-fill rule would have nothing to pick.
        seen = {str(r["canonical_id"]) for r in candidates}
        for pos in list(needs.open_starters) + [p for elig in needs.open_flex for p in elig]:
            best = next((r for r in allowed if r.get("position") == pos), None)
            if best is not None and str(best["canonical_id"]) not in seen:
                candidates.append(best)
                seen.add(str(best["canonical_id"]))
        if not candidates:
            candidates = available[:CANDIDATE_POOL] or available
        if not candidates:
            raise ValueError("bot room: the board is empty")

        room = room_state(self._made, [(str(r.get("position", "")), r.get("adp")) for r in available])
        my_rbs = [
            r for r in team.picks
            if r.get("position") == "RB" and int(r.get("pos_rank") or 999) <= HANDCUFF_RB_RANK
        ]
        my_rb_teams = {str(r.get("team", "")) for r in my_rbs if r.get("team")}
        draft_fraction = (round_no - 1) / max(1, self._rounds)
        starter_byes: dict[int, int] = {}
        for r in team.picks:
            bye = r.get("bye_week")
            if isinstance(bye, int):
                starter_byes[bye] = starter_byes.get(bye, 0) + 1

        best_row, best_u = None, -float("inf")
        for row in candidates:
            pos = str(row.get("position", ""))
            adp = row.get("adp")
            market = -(float(adp) if adp is not None else 10_000.0 + float(row["overall_rank"]))
            if adp is None:
                # Unpriced: treat the board rank as the market's number, but the
                # bot is *less* sure about it — leave the market term at rank.
                market = -float(row["overall_rank"])
            value = -float(row["overall_rank"])
            u = (1.0 - prof.value_weight) * market + prof.value_weight * value

            tag = needs.tag(pos)
            if tag == NEED_STARTER:
                u += NEED_STARTER_PICKS * prof.need_weight
            elif tag == NEED_FLEX:
                u += NEED_FLEX_PICKS * prof.need_weight
            elif pos not in ("RB", "WR"):
                u -= BACKUP_PICKS  # bench-only at a one-starter position

            # Runs pull in the drafters who still *need* the position (the
            # rational panic: "my starter is disappearing"), not everyone.
            if tag in (NEED_STARTER, NEED_FLEX):
                u += RUN_PICKS * prof.run_sensitivity * room.run_excess.get(pos, 0.0)

            if (
                pos == "RB" and prof.handcuff > 0 and draft_fraction >= HANDCUFF_AFTER_FRACTION
                and str(row.get("team", "")) in my_rb_teams
                and int(row.get("pos_rank") or 0) > HANDCUFF_RB_RANK
            ):
                u += HANDCUFF_PICKS * prof.handcuff

            bye = row.get("bye_week")
            if isinstance(bye, int) and starter_byes.get(bye, 0) >= 2:
                u -= BYE_PICKS * prof.bye_aware * (starter_byes[bye] - 1)

            u += prof.pos_bias.get(pos, 0.0)
            pos_rank = int(row.get("pos_rank") or 999)
            if pos == "QB" and prof.elite_qb > 0 and pos_rank <= ELITE_POS_RANK and team.pos_count("QB") == 0:
                u += ELITE_PICKS * prof.elite_qb
            if pos == "TE" and prof.elite_te > 0 and pos_rank <= ELITE_POS_RANK and team.pos_count("TE") == 0:
                u += ELITE_PICKS * prof.elite_te

            u += self._rng.gauss(0.0, prof.reach * NOISE_SLOPE * pick_no)
            if u > best_u:
                best_row, best_u = row, u
        assert best_row is not None
        return best_row
