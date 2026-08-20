"""The live draft loop: poll → rebuild → recompute → publish (M5, framework §7).

One :class:`DraftLoop` owns the whole cycle the companion runs every ~2.5s:

1. **Poll** the pick source (live: ``YahooClient.get_draft_results``, which
   bypasses the warm cache; the client's shared throttle is what keeps the loop
   at the Yahoo-safe draft rate — this loop adds its own sleep between polls
   but never bypasses that throttle).
2. **Rebuild** the drafted set from the *current* made-pick list via
   :class:`~fantasy_coach.draft.state.DraftState` (undo-safe by construction),
   and mirror the picks into the store (``clear + record``, so the store's
   ``draft_picks`` table is also rebuild-not-append).
3. **Recompute** the available board by filtering drafted players out of the
   stored projections/players and re-running
   :func:`~fantasy_coach.value.board.build_value_board` — replacement baselines
   shift as pools drain, exactly the M4 design seam — then re-rank by roster
   need and rebuild the recommendation.
4. **Publish** an immutable JSON-ready snapshot for the web layer. Snapshots
   carry their own age; a reader that sees one older than ``stale_after``
   (poll failures, network wobble) must treat the recommendation as stale —
   the page greys it out rather than show a pick based on an old room state.

Steps 2–3 only run when the made-pick list actually changed (§7 "diff, don't
re-pull"), so an idle clock costs one HTTP GET and nothing else.

Everything is injectable (clock, sleep, source, store) so the whole loop runs
offline in tests and in ``--simulate`` mode with zero Yahoo dependency.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Sequence
from typing import Protocol

from fantasy_coach.clients.models import DraftPick, LeagueSettings
from fantasy_coach.clients.throttle import DRAFT_POLL_INTERVAL
from fantasy_coach.clients.yahoo import YahooClient
from fantasy_coach.draft.recommend import (
    Recommendation,
    RankedPlayer,
    RosterNeeds,
    assign_roster,
    build_recommendation,
    compute_needs,
    need_weight,
    rank_available,
    roster_slots,
)
from fantasy_coach.draft.state import (
    DraftState,
    parse_off_board_id,
    snake_team_for_pick,
)
from fantasy_coach.draft.survival import (
    RoomState,
    estimate_survival,
    need_scale,
    room_state,
)
from fantasy_coach.ingest.canonical import CanonicalPlayer
from fantasy_coach.ingest.injury import InjuryReport, merge_reports
from fantasy_coach.ingest.schedule import SeasonSchedule
from fantasy_coach.league import KeeperRules, keeper_note
from fantasy_coach.store.store import CoachStore
from fantasy_coach.value.board import (
    STREAMING_POSITIONS,
    ValueBoard,
    build_value_board,
    starter_demand,
)
from fantasy_coach.value.injury import build_risk_index

__all__ = ["PickSource", "YahooPickSource", "StatusSource", "DraftLoop"]

logger = logging.getLogger(__name__)

#: Snapshot ``stale`` trips after this many seconds without a successful poll —
#: generous enough for one failed poll + retry backoff, tight enough that a
#: dead connection can't quietly serve a five-minute-old recommendation.
DEFAULT_STALE_AFTER = 12.0

#: Positions the tier columns surface, in display order.
_TIER_POSITIONS = ("QB", "RB", "WR", "TE")

#: How fast a streamable position's need weight ramps to full urgency at the
#: endgame: urgency = 1 − (spare picks beyond open starters) / ramp. With 5
#: spare picks the IDP/DEF slots can comfortably wait; at 0 spare picks every
#: pick must fill a starter and the weight is full.
STREAM_URGENCY_RAMP = 5.0

#: Default seconds between live injury-status re-checks. Deliberately much
#: slower than the pick poll: the status source (Sleeper's full players blob)
#: is a big pull, statuses change on the minutes scale, and politeness is a
#: framework principle (§7) — the *pick* cadence stays 2.5s regardless.
DEFAULT_STATUS_INTERVAL = 120.0


def _unmapped_name(raw_id: str) -> str:
    """Display name for a pick with no canonical identity (off-board aware)."""
    off = parse_off_board_id(raw_id)
    if off is None:
        return f"id {raw_id}"
    position, label = off
    return label or (f"Off-board {position}" if position else "Off-board pick")


def _unmapped_position(raw_id: str) -> str:
    """Display position for a pick with no canonical identity."""
    off = parse_off_board_id(raw_id)
    return (off[0] or "?") if off else "?"


class PickSource(Protocol):
    """Anything that returns the league's current full pick list."""

    def fetch(self) -> list[DraftPick]:  # pragma: no cover - protocol
        ...


class StatusSource(Protocol):
    """A live injury-status feed: ``{canonical_id: report}`` per fetch.

    The concrete one is
    :class:`~fantasy_coach.ingest.injury.SleeperStatusSource` (free, keyless,
    frequently updated); the protocol keeps tests offline and Yahoo swappable.
    """

    name: str

    def fetch(self) -> dict[str, InjuryReport]:  # pragma: no cover - protocol
        ...


class YahooPickSource:
    """The live source: ``get_draft_results`` (cache-bypassed, throttled)."""

    def __init__(self, yahoo: YahooClient, league_key: str) -> None:
        self._yahoo = yahoo
        self._league_key = league_key

    def fetch(self) -> list[DraftPick]:
        """One live poll of the draft room."""
        return self._yahoo.get_draft_results(self._league_key)


class DraftLoop:
    """Owns the poll → rebuild → recommend → snapshot cycle.

    Args:
        store: The warmed :class:`CoachStore` (projections/players/settings in).
        settings: The league's settings (drives baselines and roster needs).
        source: Where picks come from — live Yahoo or a simulation.
        my_team_key: The founder's team; drives roster-need weighting.
        league_key: Defaults to ``settings.league_key``.
        mode: ``"live"`` or ``"simulation"`` — display only.
        poll_interval: Sleep between polls. The Yahoo client's throttle is the
            hard rate floor; this just paces the loop.
        stale_after: Seconds without a successful poll before snapshots flag
            themselves stale.
        team_names: Optional ``{team_key: display name}`` for the UI.
        record_to_store: Mirror picks into ``store.draft_picks`` each change.
        season: Projection season to load; default = newest stored.
        schedule: Optional season schedule + opponent difficulty (step 5) —
            enables playoff values, schedule notes, and the bye-stacking nudge.
        playoff_weight: Blend weight for the board's draft value (0.0 = pure
            season VORP, the pre-step-5 behavior).
        status_source: Optional live injury-status feed (step 6). Re-checked
            every ``status_interval`` seconds inside the normal poll cycle; a
            changed designation forces a board recompute, so a player ruled
            out mid-draft drops in real time. The store's Yahoo/Sleeper
            reports from the warm pass are the baseline the live feed merges
            over (fresh beats stale, severe beats mild).
        status_interval: Seconds between live status re-checks (the pick poll
            keeps its own, much faster cadence).
        injury_weight: How hard the injury/durability discount shades draft
            values on the live board (0.0 = flags only, ranking unchanged).
        risk_preference: Floor↔ceiling tilt on the live board (upgrade 1;
            0.0 = median, <0 safe, >0 upside).
        sos_weight: Per-week SOS mix on the live board (upgrade 3; 0.0 = off).
        draft_order: Optional round-1 team order (``[team_key, …]``) for
            predicting whose pick is whose before round 1 has been observed
            — the simulator knows its own order; live mode learns it from
            Yahoo's prefilled ``team_key``s or the first round.
        keeper_rules: The league's keeper mechanics, when it is a keeper
            league — the recommendation then says whether the pick on the
            clock will be keepable next year and at what cost.
        time_func / sleep_func: Injectable clock (tests never sleep).

    Keeper picks: Yahoo pre-populates each kept player as a *made* pick in
    the round it costs — possibly far ahead of the pick on the clock. The
    loop therefore reads "the current pick" as the first **unmade** pick in
    the list (not made+1) and skips already-made picks when predicting your
    next turns, so a keeper in your round 6 is correctly not "your next
    pick" (the simulator scripts keepers the same way).
    """

    def __init__(
        self,
        store: CoachStore,
        settings: LeagueSettings,
        source: PickSource,
        *,
        my_team_key: str,
        league_key: str | None = None,
        mode: str = "live",
        poll_interval: float = DRAFT_POLL_INTERVAL,
        stale_after: float = DEFAULT_STALE_AFTER,
        team_names: dict[str, str] | None = None,
        record_to_store: bool = True,
        season: int | None = None,
        schedule: SeasonSchedule | None = None,
        playoff_weight: float = 0.0,
        status_source: StatusSource | None = None,
        status_interval: float = DEFAULT_STATUS_INTERVAL,
        injury_weight: float = 0.0,
        risk_preference: float = 0.0,
        sos_weight: float = 0.0,
        draft_order: Sequence[str] | None = None,
        keeper_rules: KeeperRules | None = None,
        time_func: Callable[[], float] = time.time,
        sleep_func: Callable[[float], None] = time.sleep,
    ) -> None:
        self._store = store
        self._settings = settings
        self._source = source
        self._schedule = schedule
        self._playoff_weight = playoff_weight
        self._status_source = status_source
        self._status_interval = status_interval
        self._injury_weight = injury_weight
        self._risk_preference = risk_preference
        self._sos_weight = sos_weight
        self._draft_order = list(draft_order or [])
        self._keeper_rules = keeper_rules
        self.league_key = league_key or settings.league_key
        self.mode = mode
        self.poll_interval = poll_interval
        self._stale_after = stale_after
        self._team_names = dict(team_names or {})
        self._record_to_store = record_to_store
        self._time = time_func
        self._sleep = sleep_func

        if season is None:
            row = store.sql("SELECT MAX(season) AS s FROM projections")
            season = row[0]["s"] if row and row[0]["s"] is not None else None
        # When several projection sources coexist for the season (a
        # nflverse↔consensus switch leaves both), load only the freshest one —
        # an unfiltered read would put every player on the board twice.
        src_rows = store.sql(
            "SELECT source, MAX(updated_at) AS u FROM projections "
            "WHERE season IS ? AND horizon = 'season' GROUP BY source "
            "ORDER BY u DESC, source",
            [season],
        )
        proj_source = src_rows[0]["source"] if src_rows else None
        self._projections = store.projection_records(season=season, source=proj_source)
        self._players: list[CanonicalPlayer] = store.canonical_players()
        self._by_canonical = {p.canonical_id: p for p in self._players}
        self._adp_by_canonical = {
            p.canonical_id: (p.market.adp, p.market.adp_stddev) for p in self._players
        }
        self._num_teams = settings.max_teams or 12

        # Injury picture (step 6): per-source reports seeded from the store's
        # warm pass; the live status source overwrites its own slice each
        # re-check and the cross-source merge re-runs.
        self._reports_by_source = store.injury_reports_by_source()
        self._durability = store.durability_profiles()
        self._merged_reports = self._merge_all_reports()
        self._last_status_poll: float | None = None

        self.state = DraftState(
            self._players, league_key=self.league_key, my_team_key=my_team_key
        )

        self._lock = threading.Lock()
        self._poll_lock = threading.RLock()  # poll_once from the loop thread + manual entry
        self._board: ValueBoard | None = None
        self._slots: list = []
        self._ranked: list[RankedPlayer] = []
        self._stream_urgency_map: dict[str, float] = {}
        self._recommendation: Recommendation | None = None
        self._room: RoomState = RoomState()
        self._my_upcoming: list[int] = []
        self._team_slots: dict[str, list] = {}  # every team's assigned roster slots
        self._team_needs: dict[str, RosterNeeds] = {}
        self._keeper_labels: dict[str, str] = {}  # canonical_id → "keeper (Rd 6)"
        self._last_picks: list[DraftPick] = []
        self._last_sig: tuple | None = None
        self._last_success: float | None = None
        self._last_error: str | None = None
        self._snapshot: dict[str, object] = {}
        self.poll_count = 0

    # -- setup ----------------------------------------------------------------

    def seed_keepers(self, team_key: str, raw_ids: Sequence[str]) -> int:
        """Seed a team's pre-draft roster (call before :meth:`run`)."""
        return self.state.seed_keepers(team_key, raw_ids)

    @property
    def recommendation(self) -> Recommendation | None:
        """The current BEST PICK NOW (None before the first poll)."""
        return self._recommendation

    @property
    def board(self) -> ValueBoard | None:
        """The current available-only board (None before the first poll)."""
        return self._board

    # -- one cycle -------------------------------------------------------------

    def poll_once(self) -> dict[str, object]:
        """Poll, rebuild if the room changed, and publish a fresh snapshot.

        Raises whatever the source raises — :meth:`run` catches, logs, and
        keeps the last good snapshot (flagged stale once old enough).
        Serialized: manual entry calls this from the HTTP thread while the
        loop thread polls too.
        """
        with self._poll_lock:
            return self._poll_once()

    def _poll_once(self) -> dict[str, object]:
        status_changed = self._refresh_statuses()
        picks = self._source.fetch()
        self.poll_count += 1
        sig = tuple(
            (p.pick, p.player_key, p.team_key) for p in picks if p.is_made
        )
        self._last_picks = list(picks)  # before recompute: pick→team lookups
        if sig != self._last_sig or self._board is None or status_changed:
            self.state.rebuild(picks)
            if self._record_to_store:
                self._store.clear_draft_picks(self.league_key)
                self._store.record_draft_picks(self.league_key, picks)
            self._recompute()
            self._last_sig = sig
        self._last_success = self._time()
        self._last_error = None
        snapshot = self._build_snapshot()
        with self._lock:
            self._snapshot = snapshot
        return snapshot

    def _merge_all_reports(self) -> dict[str, InjuryReport]:
        """Cross-source merged status per player (fresh beats stale, severe beats mild)."""
        by_player: dict[str, list[InjuryReport]] = {}
        for reports in self._reports_by_source.values():
            for cid, report in reports.items():
                by_player.setdefault(cid, []).append(report)
        merged: dict[str, InjuryReport] = {}
        for cid, reports in by_player.items():
            winner = merge_reports(reports)
            if winner is not None:
                merged[cid] = winner
        return merged

    def _refresh_statuses(self) -> bool:
        """Re-check the live status source when due; True if any status changed.

        Failures are logged and swallowed — a broken status feed must never
        stall the pick loop. Fetched reports are mirrored into the store (its
        vintage stamp is the freshness record the founder checks).
        """
        if self._status_source is None:
            return False
        now = self._time()
        if (
            self._last_status_poll is not None
            and now - self._last_status_poll < self._status_interval
        ):
            return False
        self._last_status_poll = now
        try:
            fetched = self._status_source.fetch()
        except Exception as exc:
            logger.warning("status re-check failed (%s) — keeping last statuses", exc)
            return False
        self._reports_by_source[self._status_source.name] = fetched
        if self._record_to_store:
            self._store.upsert_injury_reports(fetched, source=self._status_source.name)
        merged = self._merge_all_reports()
        changed = {
            cid: r.status for cid, r in merged.items() if r.status
        } != {cid: r.status for cid, r in self._merged_reports.items() if r.status}
        self._merged_reports = merged
        if changed:
            logger.info("injury statuses changed — board will recompute")
        return changed

    def _recompute(self) -> None:
        """Rebuild the available board + ranking + recommendation from state."""
        drafted = self.state.drafted_canonical_ids
        projections = [r for r in self._projections if r.source_id not in drafted]
        players = [p for p in self._players if p.canonical_id not in drafted]
        risk = build_risk_index(self._merged_reports, self._durability)
        # Every team's roster (keepers + live picks) and needs come FIRST —
        # the founder's own drives the need weighting, the others feed the
        # survival model, and the sum of everyone's still-unfilled slots is
        # the **remaining starter demand** the replacement baselines run
        # against (P0-1: the pool and the demand shrink together, so
        # replacement level doesn't drift down the drained pool).
        self._team_slots = {
            key: assign_roster(roster_slots(self._settings), self._team_roster_infos(key))
            for key in self._all_team_keys()
        }
        self._team_needs = {k: compute_needs(v) for k, v in self._team_slots.items()}
        remaining_dedicated, remaining_flex = self._remaining_demand()
        self._board = build_value_board(
            projections,
            self._settings,
            num_teams=self._num_teams,
            demand=(remaining_dedicated, remaining_flex),
            players=players or None,
            schedule=self._schedule,
            playoff_weight=self._playoff_weight,
            risk=risk or None,
            injury_weight=self._injury_weight,
            risk_preference=self._risk_preference,
            sos_weight=self._sos_weight,
        )
        my_key = self.state.my_team_key
        slots = self._team_slots.get(my_key) or assign_roster(
            roster_slots(self._settings), self._my_roster_infos()
        )
        self._slots = slots
        needs = self._team_needs.get(my_key) or compute_needs(slots)

        # Survival (upgrade 2): where my next picks fall + what the room is
        # doing, then P(available) per player — recomputed every changed poll.
        current_pick = self._current_pick()
        self._my_upcoming = self._upcoming_my_picks(current_pick, count=2)
        on_clock = bool(self._my_upcoming) and self._my_upcoming[0] == current_pick
        my_next = self._my_upcoming[0] if self._my_upcoming else None
        my_after = self._my_upcoming[1] if len(self._my_upcoming) > 1 else None
        positions = {e.position for e in self._board.entries}
        room_weights = [
            {pos: need_weight(n.tag(pos), pos) for pos in positions}
            for n in self._team_needs.values()
        ]
        scale_next = need_scale(
            self._intervening_need_weights(current_pick, my_next, positions), room_weights, positions
        ) if my_next is not None else {}
        scale_after = need_scale(
            self._intervening_need_weights(current_pick, my_after, positions), room_weights, positions
        ) if my_after is not None else {}
        self._need_scale_next = scale_next
        made = [
            (
                rp.pick.pick,
                (self._by_canonical[rp.canonical_id].position if rp.canonical_id in self._by_canonical else ""),
                self._adp_by_canonical.get(rp.canonical_id or "", (None, None))[0],
            )
            for rp in self.state.resolved
        ]
        self._room = room_state(
            made, [(e.position, e.adp) for e in self._board.entries]
        )
        survival = estimate_survival(
            (
                {
                    "canonical_id": e.canonical_id,
                    "position": e.position,
                    "adp": e.adp,
                    "adp_stdev": self._adp_by_canonical.get(e.canonical_id, (None, None))[1],
                    "overall_rank": e.overall_rank,
                }
                for e in self._board.entries
            ),
            current_pick=current_pick,
            my_next_pick=my_next,
            my_pick_after=my_after,
            room=self._room,
            need_scale_next=scale_next,
            need_scale_after=scale_after,
        )
        self._stream_urgency_map = self._stream_urgency(
            needs, current_pick, remaining_dedicated, remaining_flex
        )
        self._ranked = rank_available(
            self._board,
            needs,
            starter_bye_counts=self._starter_bye_counts(slots),
            survival=survival,
            stream_urgency=self._stream_urgency_map,
        )
        # Picks between the one being decided and my following pick.
        decided = my_next if my_next is not None else current_pick
        following = my_after if on_clock else my_next
        picks_until_next = (
            following - decided - 1
            if following is not None and following > decided
            else None
        )
        self._recommendation = build_recommendation(
            self._ranked,
            needs,
            current_pick=current_pick,
            picks_until_next=picks_until_next,
            on_the_clock=on_clock,
        )
        if self._recommendation is not None and self._keeper_rules is not None:
            decided_pick = my_next if my_next is not None else current_pick
            note = keeper_note(
                (decided_pick - 1) // self._num_teams + 1, self._keeper_rules
            )
            if note:
                self._recommendation.reasons.append(note)

    def _remaining_demand(
        self,
    ) -> tuple[dict[str, int], list[tuple[tuple[str, ...], int]]]:
        """The league's still-unfilled starter demand ``(dedicated, flex)``.

        Sums every known team's open slots (:attr:`_team_needs`); teams the
        loop cannot see yet (live mode before the order is known) count at
        full per-team demand — they have no attributed picks, so nothing of
        theirs is filled.
        """
        dedicated: dict[str, int] = {}
        flex_counts: dict[tuple[str, ...], int] = {}
        for needs in self._team_needs.values():
            for pos, n in needs.open_starters.items():
                dedicated[pos] = dedicated.get(pos, 0) + n
            for elig in needs.open_flex:
                flex_counts[elig] = flex_counts.get(elig, 0) + 1
        missing = max(0, self._num_teams - len(self._team_needs))
        if missing:
            ded_one, flex_one = starter_demand(self._settings, 1)
            for pos, n in ded_one.items():
                dedicated[pos] = dedicated.get(pos, 0) + n * missing
            for elig, n in flex_one:
                flex_counts[elig] = flex_counts.get(elig, 0) + n * missing
        return dedicated, list(flex_counts.items())

    def _stream_urgency(
        self,
        needs: RosterNeeds,
        current_pick: int,
        dedicated: dict[str, int],
        flex: list[tuple[tuple[str, ...], int]],
    ) -> dict[str, float]:
        """Urgency in ``[0, 1]`` per streamable position on the board (P0-3).

        Mirrors how a sharp drafter (and :mod:`~fantasy_coach.draft.bots`'
        ``IDP_ROUNDS_LEFT``) treats IDP/DEF/K: wait, unless (a) the endgame is
        here — my spare picks beyond my open starting slots are running out —
        or (b) the remaining startable supply at the position is approaching
        the league's remaining demand (a late DEF run in a 32-DEF world).
        """
        positions = (
            {e.position for e in self._board.entries} & STREAMING_POSITIONS
            if self._board is not None
            else set()
        )
        if not positions:
            return {}
        upcoming = (
            self._upcoming_my_picks(current_pick, count=10_000)
            if self._last_picks
            else []
        )
        my_left = len(upcoming)
        my_open = sum(needs.open_starters.values()) + len(needs.open_flex)
        time_u = 0.0
        if my_left:
            slack = my_left - my_open
            time_u = max(0.0, min(1.0, 1.0 - slack / STREAM_URGENCY_RAMP))
        supply_by_pos: dict[str, int] = {}
        for e in self._board.entries:
            supply_by_pos[e.position] = supply_by_pos.get(e.position, 0) + 1
        out: dict[str, float] = {}
        for pos in positions:
            demand_p = dedicated.get(pos, 0) + sum(
                n for elig, n in flex if pos in elig
            )
            supply_u = 0.0
            if demand_p > 0:
                supply = supply_by_pos.get(pos, 0)
                supply_u = max(0.0, min(1.0, 1.0 - (supply - demand_p) / demand_p))
            out[pos] = round(max(time_u, supply_u), 3)
        return out

    def _all_team_keys(self) -> list[str]:
        """Every team in round-1 order (configured, observed, or seen in picks)."""
        order = self._round1_order()
        if order:
            return list(order)
        seen: list[str] = []
        for p in self._last_picks:
            if p.team_key and p.team_key not in seen:
                seen.append(p.team_key)
        for key in self.state.keeper_teams():
            if key not in seen:
                seen.append(key)
        if self.state.my_team_key and self.state.my_team_key not in seen:
            seen.append(self.state.my_team_key)
        return seen

    def _intervening_need_weights(
        self, current_pick: int, target_pick: int | None, positions: set[str]
    ) -> list[dict[str, float]]:
        """Need weights of the teams whose live picks fall in ``[current, target)``."""
        if target_pick is None:
            return []
        made = {p.pick for p in self._last_picks if p.is_made}
        out: list[dict[str, float]] = []
        for n in range(current_pick, target_pick):
            if n in made:
                continue  # a pre-made keeper pick isn't a live selection
            key = self._team_for_pick(n)
            if not key or key == self.state.my_team_key:
                continue
            needs = self._team_needs.get(key)
            if needs is None:
                continue
            out.append({pos: need_weight(needs.tag(pos), pos) for pos in positions})
        return out

    def _current_pick(self) -> int:
        """The pick on the clock: the first unmade pick (keeper picks may be
        pre-made further down the list), else made + 1."""
        for p in sorted(self._last_picks, key=lambda p: p.pick):
            if not p.is_made:
                return p.pick
        return self.state.pick_count + 1

    def _upcoming_my_picks(self, current_pick: int, count: int = 2) -> list[int]:
        """My next ``count`` pick numbers from ``current_pick`` on (inclusive).

        Uses Yahoo's prefilled team keys, then the observed round-1 order,
        then the configured ``draft_order`` (simulation). Empty when the
        order is not knowable yet.
        """
        total = len(self._last_picks) if self._last_picks else None
        if total is None:
            return []
        my_key = self.state.my_team_key
        made = {p.pick for p in self._last_picks if p.is_made}
        out: list[int] = []
        for n in range(current_pick, total + 1):
            if n in made and n != current_pick:
                continue  # a pre-made keeper pick is not a turn on the clock
            if self._team_for_pick(n) == my_key:
                out.append(n)
                if len(out) >= count:
                    break
        return out

    @staticmethod
    def _starter_bye_counts(slots: Sequence) -> dict[int, int]:
        """``{bye_week: starters sharing it}`` from my filled starting slots."""
        counts: dict[int, int] = {}
        for slot in slots:
            if slot.is_bench or slot.player is None:
                continue
            bye = slot.player.get("bye")
            if isinstance(bye, int):
                counts[bye] = counts.get(bye, 0) + 1
        return counts

    # -- the loop --------------------------------------------------------------

    def run(
        self,
        stop: threading.Event | None = None,
        *,
        max_polls: int | None = None,
    ) -> None:
        """Poll forever (or until ``stop`` / ``max_polls``), surviving errors.

        A failed poll is logged and the loop keeps going — the previous
        snapshot stays up and flags itself stale via its own age. Yahoo is
        ground truth; we never fabricate state to paper over a gap.
        """
        polls = 0
        while not (stop is not None and stop.is_set()):
            try:
                self.poll_once()
            except Exception as exc:
                self._last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("draft poll failed (%s) — keeping last state", exc)
                with self._lock:
                    if self._snapshot:
                        self._snapshot = {**self._snapshot, "error": self._last_error}
            polls += 1
            if max_polls is not None and polls >= max_polls:
                break
            self._sleep(self.poll_interval)

    # -- snapshot --------------------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        """The latest published snapshot with live freshness fields.

        Thread-safe; this is what the web layer serves. ``stale`` means the
        recommendation must not be trusted for an actual pick right now.
        """
        with self._lock:
            snap = dict(self._snapshot)
        now = self._time()
        age = (now - self._last_success) if self._last_success is not None else None
        snap["age_seconds"] = round(age, 1) if age is not None else None
        snap["stale"] = age is None or age > self._stale_after
        return snap

    # -- snapshot assembly ------------------------------------------------------

    def _team_name(self, team_key: str) -> str:
        return self._team_names.get(team_key) or team_key.rsplit(".", 1)[-1]

    def _bye_for(self, p: CanonicalPlayer) -> int | None:
        """A player's bye week, falling back to the schedule's team bye.

        Offline stores synthesize players from projections and carry no
        per-player byes — without the fallback the bye-stacking nudge could
        never fire in a simulated dress rehearsal.
        """
        if p.bye_week is not None:
            return p.bye_week
        if self._schedule is not None and p.team:
            return self._schedule.bye_week(p.team)
        return None

    def _my_roster_infos(self) -> list[dict[str, object]]:
        """Display dicts for my keepers + picks, in acquisition order."""
        return self._team_roster_infos(self.state.my_team_key)

    def _team_roster_infos(self, team_key: str) -> list[dict[str, object]]:
        """Display dicts for a team's keepers + picks, in acquisition order.

        Keepers seeded via ``seed_keepers`` (live Yahoo rosters) come first;
        pre-made keeper picks in the pick list (manual / simulated drafts)
        arrive as picks and are labelled through ``keeper_labels``.
        """
        infos: list[dict[str, object]] = []
        for cid, unmapped, pick_no, raw_id in self.state.team_acquisitions(team_key):
            if unmapped:
                # Off-board entries carry their position in the raw id, so the
                # pick still fills the right roster slot and the needs math
                # stays honest; a genuinely unknown id goes to the bench.
                off = parse_off_board_id(raw_id)
                position = off[0] if off else ""
                label = (off[1] if off else "") or (
                    f"Off-board {position}" if position else "Unmapped pick"
                )
                infos.append(
                    {"canonical_id": "", "name": label, "position": position,
                     "team": "", "bye": None, "pick": pick_no,
                     "keeper": self._keeper_labels.get(raw_id, "")}
                )
                continue
            p = self._by_canonical.get(cid or "")
            if p is None:
                continue
            infos.append(
                {
                    "canonical_id": cid,
                    "name": p.name,
                    "position": p.position,
                    "team": p.team,
                    "bye": self._bye_for(p),
                    "pick": pick_no,
                    "keeper": self._keeper_labels.get(cid, ""),
                }
            )
        return infos

    def set_keeper_labels(self, labels: dict[str, str]) -> None:
        """Label roster entries that are keepers (``{canonical_id: "Rd 6 keeper"}``)."""
        self._keeper_labels = dict(labels)

    def _round1_order(self) -> list[str]:
        order = [
            rp.pick.team_key
            for rp in self.state.resolved
            if rp.pick.round == 1 and rp.pick.team_key
        ]
        if len(order) == self._num_teams:
            return order
        return self._draft_order if len(self._draft_order) == self._num_teams else []

    def _team_for_pick(self, pick_number: int) -> str | None:
        """Prefer Yahoo's own team_key on the (unmade) pick; else snake-predict."""
        for p in self._last_picks:
            if p.pick == pick_number and p.team_key:
                return p.team_key
        return snake_team_for_pick(self._round1_order(), pick_number)

    def _teams_snapshot(self, current_pick: int | None) -> list[dict[str, object]]:
        """Every team's roster + needs + next live pick (the league view)."""
        my_key = self.state.my_team_key
        made = {p.pick for p in self._last_picks if p.is_made}
        total = len(self._last_picks)
        out: list[dict[str, object]] = []
        for key in self._all_team_keys():
            slots = self._team_slots.get(key, [])
            needs = self._team_needs.get(key)
            next_pick = None
            if current_pick is not None:
                for n in range(current_pick, total + 1):
                    if n in made and n != current_pick:
                        continue
                    if self._team_for_pick(n) == key:
                        next_pick = n
                        break
            out.append(
                {
                    "team_key": key,
                    "name": self._team_name(key),
                    "is_me": key == my_key,
                    "roster": [
                        {"label": s.label, "is_bench": s.is_bench, "player": s.player}
                        for s in slots
                    ],
                    "filled": sum(1 for s in slots if s.player is not None),
                    "needs": (
                        {
                            "open_starters": dict(needs.open_starters),
                            "open_flex": ["/".join(e) for e in needs.open_flex],
                            "open_bench": needs.open_bench,
                        }
                        if needs
                        else None
                    ),
                    "next_pick": next_pick,
                }
            )
        return out

    def _likely_gone(self, limit: int = 8) -> list[dict[str, object]]:
        """Top-ranked available players unlikely to reach my next pick.

        The "your pick is in N — these are probably gone by then" list: from
        the need-weighted ranking, players whose survival to my next pick
        (or, on the clock, to the following one) is below the coin-flip
        line, in ranking order.
        """
        from fantasy_coach.draft.survival import COIN_FLIP_BELOW  # noqa: PLC0415

        on_clock = bool(self._my_upcoming) and self._my_upcoming[0] == self._current_pick()
        out: list[dict[str, object]] = []
        for rp in self._ranked[:40]:
            sv = rp.survival
            if sv is None:
                continue
            p = sv.p_after if on_clock else sv.p_next
            if p is None or p >= COIN_FLIP_BELOW:
                continue
            out.append(
                {
                    "canonical_id": rp.entry.canonical_id,
                    "name": rp.entry.name,
                    "position": rp.entry.position,
                    "score": round(rp.score, 1),
                    "p": round(p, 3),
                }
            )
            if len(out) >= limit:
                break
        return out

    def _build_snapshot(self) -> dict[str, object]:
        made = self.state.pick_count
        total = len(self._last_picks) if self._last_picks else None
        complete = total is not None and made >= total
        current_pick = self._current_pick() if not complete else made
        num = self._num_teams
        current_round = (current_pick - 1) // num + 1
        pick_in_round = (current_pick - 1) % num + 1

        on_clock_key = None if complete else self._team_for_pick(current_pick)
        my_key = self.state.my_team_key
        my_next: int | None = None
        my_after: int | None = None
        if not complete and total is not None:
            upcoming = self._upcoming_my_picks(current_pick, count=2)
            my_next = upcoming[0] if upcoming else None
            my_after = upcoming[1] if len(upcoming) > 1 else None

        recent = []
        for rp in self.state.resolved[-12:][::-1]:
            player = self._by_canonical.get(rp.canonical_id or "")
            recent.append(
                {
                    "pick": rp.pick.pick,
                    "round": rp.pick.round,
                    "team_key": rp.pick.team_key,
                    "team": self._team_name(rp.pick.team_key),
                    "is_me": rp.pick.team_key == my_key,
                    "name": player.name if player else _unmapped_name(rp.raw_id),
                    "position": (
                        player.position
                        if player
                        else _unmapped_position(rp.raw_id)
                    ),
                    "unmapped": player is None,
                }
            )

        tiers: dict[str, object] = {}
        board = self._board
        by_pos: dict[str, list[RankedPlayer]] = {}
        for rp in self._ranked:
            by_pos.setdefault(rp.entry.position, []).append(rp)
        for pos in _TIER_POSITIONS:
            pos_players = sorted(
                by_pos.get(pos, []), key=lambda rp: rp.entry.pos_rank
            )[:12]
            groups: list[dict[str, object]] = []
            for rp in pos_players:
                if not groups or groups[-1]["tier"] != rp.entry.tier:
                    groups.append({"tier": rp.entry.tier, "players": []})
                groups[-1]["players"].append(rp.as_dict())
            tiers[pos] = {
                "baseline": (board.baselines.get(pos) if board else None),
                "groups": groups,
            }

        rec = self._recommendation
        return {
            "mode": self.mode,
            "league_key": self.league_key,
            "my_team_key": my_key,
            "my_team_name": self._team_name(my_key) if my_key else "",
            "poll_interval": self.poll_interval,
            "error": self._last_error,
            "playoff": {
                "weight": self._playoff_weight if self._schedule else 0.0,
                "weeks": board.playoff_weeks if board else [],
                "schedule_loaded": self._schedule is not None,
            },
            "injury": {
                "weight": self._injury_weight,
                "status_source": (
                    self._status_source.name if self._status_source else None
                ),
                "status_poll_age": (
                    round(self._time() - self._last_status_poll, 1)
                    if self._last_status_poll is not None
                    else None
                ),
                "flagged_count": sum(
                    1 for r in self._merged_reports.values() if r.status
                ),
            },
            "vintage": [
                {"scope": row["scope"], "refreshed_at": row["refreshed_at"]}
                for row in self._store.vintage()
            ],
            "draft": {
                "pick_count": made,
                "total_picks": total,
                "current_pick": current_pick,
                "round": current_round,
                "pick_in_round": pick_in_round,
                "num_teams": num,
                "complete": complete,
                "on_the_clock": (
                    {
                        "team_key": on_clock_key,
                        "team": self._team_name(on_clock_key),
                        "is_me": on_clock_key == my_key,
                    }
                    if on_clock_key
                    else None
                ),
                "my_next_pick": my_next,
                "my_pick_after": my_after,
                "picks_until_mine": (
                    my_next - current_pick if my_next is not None else None
                ),
            },
            "survival": {
                "drift": round(self._room.drift, 1),
                "runs": {p: round(x, 2) for p, x in self._room.run_excess.items()},
                "recent_positions": list(self._room.recent_positions),
            },
            "dials": {
                "playoff_weight": self._playoff_weight if self._schedule else 0.0,
                "injury_weight": self._injury_weight,
                "risk_preference": self._risk_preference,
                "sos_weight": self._sos_weight if self._schedule else 0.0,
            },
            "likely_gone": self._likely_gone(),
            "teams": self._teams_snapshot(current_pick if not complete else None),
            "recommendation": (
                {
                    **rec.player.as_dict(),
                    "reasons": rec.reasons,
                    "swapped_from": (
                        rec.swapped_from.as_dict() if rec.swapped_from else None
                    ),
                }
                if rec
                else None
            ),
            "runners_up": [rp.as_dict() for rp in self._ranked[1:4]],
            "available": [rp.as_dict() for rp in self._ranked[:40]],
            "tiers": tiers,
            "roster": [
                {
                    "label": s.label,
                    "is_flex": s.is_flex,
                    "is_bench": s.is_bench,
                    "player": s.player,
                }
                for s in self._slots
            ],
            "recent_picks": recent,
            "unmapped_count": len(self.state.unmapped),
            "available_count": len(self._ranked),
        }
