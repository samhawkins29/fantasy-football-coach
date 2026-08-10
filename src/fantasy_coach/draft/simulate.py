"""Simulation / replay mode — the whole companion, exercised without a draft.

The founder cannot debug this thing on draft night (task rule #5), so the
simulator feeds the *same* :class:`~fantasy_coach.draft.loop.DraftLoop` a pick
list shaped exactly like Yahoo's: the **full** list up front, unmade picks
blank, one more pick "made" per poll. Nothing downstream can tell the
difference — pool shrinkage, baseline drift, roster filling, the web page, all
run the real code path.

Two pieces:

* :func:`script_draft` — a deterministic snake-draft script generated from the
  store's own value board. Opponents draft by market order (ADP when stored,
  board order otherwise) under light positional caps so they build plausible
  rosters instead of hoarding one position; determinism keeps tests exact.
* :class:`SimulatedPickSource` — replays any script (generated, or a completed
  league's real ``draftresults``) N picks per poll, with :meth:`rewind` to
  exercise the undo path end to end.
"""

from __future__ import annotations

from collections.abc import Sequence

from fantasy_coach.clients.models import DraftPick, LeagueSettings
from fantasy_coach.store.store import CoachStore

__all__ = ["script_draft", "SimulatedPickSource", "sim_team_names"]

#: Per-team position caps for scripted opponents: nobody drafts a third QB or a
#: second kicker in a normal room. RB/WR stay effectively uncapped (bench depth).
_POSITION_CAPS = {"QB": 2, "TE": 2, "K": 1, "DEF": 1}
_SUPERFLEX_QB_CAP = 3

#: Where scripted teams draft their K/DEF: never before this many of their
#: rounds remain (mirrors real rooms; also keeps flat-value entries from being
#: "best available" nonsense mid-draft).
_KICKER_ROUNDS_LEFT = 2


def sim_team_names(league_key: str, num_teams: int, my_slot: int) -> dict[str, str]:
    """Display names for scripted teams (``{team_key: name}``); yours is marked."""
    names = {}
    for slot in range(1, num_teams + 1):
        key = f"{league_key}.t.{slot}"
        names[key] = "You" if slot == my_slot else f"Team {slot}"
    return names


def script_draft(
    store: CoachStore,
    settings: LeagueSettings,
    *,
    league_key: str | None = None,
    num_teams: int | None = None,
    rounds: int | None = None,
) -> list[DraftPick]:
    """Generate a full deterministic snake-draft script from the stored board.

    Every scripted pick carries a ``player_key`` whose tail is the player's
    yahoo id when the store has one, else the canonical id — matching how
    :class:`~fantasy_coach.draft.state.DraftState` resolves, so the sim works
    against both online- and offline-warmed stores.
    """
    league_key = league_key or settings.league_key
    num_teams = num_teams or settings.max_teams or 12
    rounds = rounds or max(1, settings.roster_size - settings.injury_slots)
    game_code = league_key.split(".", 1)[0] or "sim"

    board = store.get_board(league_key)
    if not board:
        raise ValueError(
            f"no stored value board for {league_key} — warm the store first"
        )
    raw_id_by_canonical = {
        row["canonical_id"]: (row["yahoo_id"] or row["canonical_id"])
        for row in store.sql("SELECT canonical_id, yahoo_id FROM players")
    }
    # Market order: ADP when stored, else board rank pushed after all ADP rows.
    queue = sorted(
        board,
        key=lambda r: (
            r["adp"] if r["adp"] is not None else 10_000 + r["overall_rank"],
            r["overall_rank"],
        ),
    )

    qb_cap = _SUPERFLEX_QB_CAP if settings.is_superflex else _POSITION_CAPS["QB"]
    taken: set[str] = set()
    pos_counts: dict[str, dict[str, int]] = {}
    picks: list[DraftPick] = []

    team_order = [f"{league_key}.t.{slot}" for slot in range(1, num_teams + 1)]
    for pick_no in range(1, rounds * num_teams + 1):
        rnd, idx = divmod(pick_no - 1, num_teams)
        team = team_order[idx] if rnd % 2 == 0 else team_order[num_teams - 1 - idx]
        counts = pos_counts.setdefault(team, {})
        rounds_left = rounds - rnd

        chosen = None
        for row in queue:
            cid = row["canonical_id"]
            if cid in taken:
                continue
            pos = row["position"]
            cap = qb_cap if pos == "QB" else _POSITION_CAPS.get(pos)
            if cap is not None and counts.get(pos, 0) >= cap:
                continue
            if pos in ("K", "DEF") and rounds_left > _KICKER_ROUNDS_LEFT:
                continue
            chosen = row
            break
        if chosen is None:  # caps exhausted the queue — take best remaining
            chosen = next(r for r in queue if r["canonical_id"] not in taken)

        cid = chosen["canonical_id"]
        taken.add(cid)
        counts[chosen["position"]] = counts.get(chosen["position"], 0) + 1
        raw_id = raw_id_by_canonical.get(cid, cid)
        picks.append(
            DraftPick(
                pick=pick_no,
                round=rnd + 1,
                team_key=team,
                player_key=f"{game_code}.p.{raw_id}",
            )
        )
    return picks


class SimulatedPickSource:
    """Replays a scripted (or completed-draft) pick list like Yahoo would.

    Each :meth:`fetch` returns the **full** list — made picks intact, the rest
    blanked (empty ``player_key``/``team_key``, exactly Yahoo's live shape) —
    then advances by ``picks_per_poll`` so the next poll sees more of the room.

    Args:
        script: The complete ordered pick list to replay.
        picks_per_poll: How many picks land between polls (draft speed).
        start_made: Picks already made when the loop starts (mid-draft join).
    """

    def __init__(
        self,
        script: Sequence[DraftPick],
        *,
        picks_per_poll: int = 1,
        start_made: int = 0,
    ) -> None:
        self._script = list(script)
        self._per_poll = max(0, picks_per_poll)
        self.made = min(max(0, start_made), len(self._script))

    @property
    def total(self) -> int:
        """Total picks in the script."""
        return len(self._script)

    @property
    def exhausted(self) -> bool:
        """True once every scripted pick has been revealed."""
        return self.made >= self.total

    def fetch(self) -> list[DraftPick]:
        """The current room state; reveals the next picks *after* returning."""
        current = [
            p if i < self.made else DraftPick(pick=p.pick, round=p.round)
            for i, p in enumerate(self._script)
        ]
        self.made = min(self.total, self.made + self._per_poll)
        return current

    def rewind(self, n: int = 1) -> None:
        """Un-make the last ``n`` picks — the commissioner-undo drill."""
        self.made = max(0, self.made - n)
