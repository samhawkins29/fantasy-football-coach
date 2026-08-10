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
    assign_roster,
    build_recommendation,
    compute_needs,
    rank_available,
    roster_slots,
)
from fantasy_coach.draft.state import DraftState, snake_team_for_pick
from fantasy_coach.ingest.canonical import CanonicalPlayer
from fantasy_coach.ingest.schedule import SeasonSchedule
from fantasy_coach.store.store import CoachStore
from fantasy_coach.value.board import ValueBoard, build_value_board

__all__ = ["PickSource", "YahooPickSource", "DraftLoop"]

logger = logging.getLogger(__name__)

#: Snapshot ``stale`` trips after this many seconds without a successful poll —
#: generous enough for one failed poll + retry backoff, tight enough that a
#: dead connection can't quietly serve a five-minute-old recommendation.
DEFAULT_STALE_AFTER = 12.0

#: Positions the tier columns surface, in display order.
_TIER_POSITIONS = ("QB", "RB", "WR", "TE")


class PickSource(Protocol):
    """Anything that returns the league's current full pick list."""

    def fetch(self) -> list[DraftPick]:  # pragma: no cover - protocol
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
        time_func / sleep_func: Injectable clock (tests never sleep).
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
        time_func: Callable[[], float] = time.time,
        sleep_func: Callable[[float], None] = time.sleep,
    ) -> None:
        self._store = store
        self._settings = settings
        self._source = source
        self._schedule = schedule
        self._playoff_weight = playoff_weight
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
        self._projections = store.projection_records(season=season)
        self._players: list[CanonicalPlayer] = store.canonical_players()
        self._by_canonical = {p.canonical_id: p for p in self._players}
        self._num_teams = settings.max_teams or 12

        self.state = DraftState(
            self._players, league_key=self.league_key, my_team_key=my_team_key
        )

        self._lock = threading.Lock()
        self._board: ValueBoard | None = None
        self._slots: list = []
        self._ranked: list[RankedPlayer] = []
        self._recommendation: Recommendation | None = None
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
        """
        picks = self._source.fetch()
        self.poll_count += 1
        sig = tuple(
            (p.pick, p.player_key, p.team_key) for p in picks if p.is_made
        )
        if sig != self._last_sig or self._board is None:
            self.state.rebuild(picks)
            if self._record_to_store:
                self._store.clear_draft_picks(self.league_key)
                self._store.record_draft_picks(self.league_key, picks)
            self._recompute()
            self._last_sig = sig
        self._last_picks = list(picks)
        self._last_success = self._time()
        self._last_error = None
        snapshot = self._build_snapshot()
        with self._lock:
            self._snapshot = snapshot
        return snapshot

    def _recompute(self) -> None:
        """Rebuild the available board + ranking + recommendation from state."""
        drafted = self.state.drafted_canonical_ids
        projections = [r for r in self._projections if r.source_id not in drafted]
        players = [p for p in self._players if p.canonical_id not in drafted]
        self._board = build_value_board(
            projections,
            self._settings,
            num_teams=self._num_teams,
            players=players or None,
            schedule=self._schedule,
            playoff_weight=self._playoff_weight,
        )
        slots = assign_roster(roster_slots(self._settings), self._my_roster_infos())
        self._slots = slots
        needs = compute_needs(slots)
        self._ranked = rank_available(
            self._board, needs, starter_bye_counts=self._starter_bye_counts(slots)
        )
        self._recommendation = build_recommendation(
            self._ranked, needs, current_pick=self.state.pick_count + 1
        )

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

    def _my_roster_infos(self) -> list[dict[str, object]]:
        """Display dicts for my keepers + picks, in acquisition order."""
        infos: list[dict[str, object]] = []
        for cid in self.state.my_canonical_ids():
            p = self._by_canonical.get(cid)
            if p is None:
                continue
            infos.append(
                {
                    "canonical_id": cid,
                    "name": p.name,
                    "position": p.position,
                    "team": p.team,
                    "bye": p.bye_week,
                }
            )
        for _ in range(self.state.my_unmapped_count()):
            infos.append(
                {"canonical_id": "", "name": "Unmapped pick", "position": "", "team": "", "bye": None}
            )
        return infos

    def _round1_order(self) -> list[str]:
        order = [
            rp.pick.team_key
            for rp in self.state.resolved
            if rp.pick.round == 1 and rp.pick.team_key
        ]
        return order if len(order) == self._num_teams else []

    def _team_for_pick(self, pick_number: int) -> str | None:
        """Prefer Yahoo's own team_key on the (unmade) pick; else snake-predict."""
        for p in self._last_picks:
            if p.pick == pick_number and p.team_key:
                return p.team_key
        return snake_team_for_pick(self._round1_order(), pick_number)

    def _build_snapshot(self) -> dict[str, object]:
        made = self.state.pick_count
        total = len(self._last_picks) if self._last_picks else None
        complete = total is not None and made >= total
        current_pick = made + 1 if not complete else made
        num = self._num_teams
        current_round = (current_pick - 1) // num + 1
        pick_in_round = (current_pick - 1) % num + 1

        on_clock_key = None if complete else self._team_for_pick(current_pick)
        my_key = self.state.my_team_key
        my_next: int | None = None
        if not complete and total is not None:
            for n in range(current_pick, total + 1):
                if self._team_for_pick(n) == my_key:
                    my_next = n
                    break

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
                    "name": player.name if player else f"id {rp.raw_id}",
                    "position": player.position if player else "?",
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
                "picks_until_mine": (
                    my_next - current_pick if my_next is not None else None
                ),
            },
            "recommendation": (
                {**rec.player.as_dict(), "reasons": rec.reasons} if rec else None
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
