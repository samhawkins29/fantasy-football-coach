"""Injury status + durability signals (step-6 input, framework §2.5 "Injury/news").

Two distinct facts about a player, from two distinct kinds of source, both
deliberately presented as *signals*, never predictions:

* **Current status** — the league-official injury designation right now
  (Questionable / Doubtful / Out / IR / PUP / …). Yahoo carries it on every
  ``Player.status`` (live during the draft); Sleeper's free, keyless players
  blob carries ``injury_status`` + practice participation and updates
  frequently. Both are normalized to one small code set here so they can be
  **merged**: the fresher report wins outright when one source is meaningfully
  newer (a player cleared this morning must beat last night's OUT), otherwise
  the most severe designation wins — and every report keeps its source label.
* **Durability history** — how much football the player has actually missed:
  games missed per season from nflverse weekly rows, plus injury-report
  designations and soft-tissue mentions from the nflverse injuries feed. This
  is turned into a transparent per-player :class:`DurabilityProfile` with a
  clamped availability discount and a categorical risk flag.

Honesty posture (:data:`DURABILITY_NOTE`, stamped into every cache file):
**re-injury cannot be truly predicted.** Games missed in the past is a real,
checkable fact; "will miss games again" is not. So the discount is clamped
tight (:data:`DURABILITY_DISCOUNT_CAP`), the flag is categorical rather than a
fake-precise probability, and the projection layer already regresses projected
games toward history — this signal is a *nudge on top*, sized accordingly.

**Freshness.** Yahoo status is live whenever Yahoo is polled; Sleeper is
frequently updated and free to re-pull on demand; nflverse durability data is
periodic (it only changes weekly in-season, never in August). Every report
carries ``fetched_at`` and every store write stamps ``data_vintage`` so the
founder can always see which vintage they are acting on (the ``refresh``
command re-pulls all of it).

Caching follows the projections/schedule pattern: :meth:`DurabilitySource.warm_cache`
while online, :meth:`DurabilitySource.load` serves the JSON cache with zero
network on draft day (framework §7).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from fantasy_coach.ingest.names import normalize_position
from fantasy_coach.ingest.sources import NflverseSource, SleeperSource

__all__ = [
    "STATUS_HEALTHY",
    "STATUS_SEVERITY",
    "STATUS_LABELS",
    "FRESHNESS_WINDOW_HOURS",
    "RISK_LOW",
    "RISK_MODERATE",
    "RISK_HIGH",
    "RISK_ELEVATED",
    "RISK_ORDER",
    "DURABILITY_NOTE",
    "DURABILITY_DISCOUNT_CAP",
    "InjuryReport",
    "normalize_status",
    "merge_reports",
    "SleeperInjury",
    "sleeper_injuries",
    "SleeperStatusSource",
    "DurabilityProfile",
    "durability_discount",
    "risk_flag",
    "DurabilitySource",
]

#: The normalized "no designation" status (active / healthy / probable).
STATUS_HEALTHY = ""

#: Severity order for the cross-source merge — higher = worse. ``NA`` (not
#: active / did-not-report) sits above OUT because it usually means "nowhere
#: near playing"; the reserve lists (PUP/NFI/IR) are the most unavailable.
STATUS_SEVERITY: dict[str, int] = {
    STATUS_HEALTHY: 0,
    "Q": 1,
    "D": 2,
    "SUS": 3,
    "O": 4,
    "NA": 5,
    "PUP": 6,
    "NFI": 6,
    "IR": 7,
}

#: Display names for the normalized codes (UI badges / reasons).
STATUS_LABELS: dict[str, str] = {
    STATUS_HEALTHY: "",
    "Q": "QUESTIONABLE",
    "D": "DOUBTFUL",
    "SUS": "SUSPENDED",
    "O": "OUT",
    "NA": "INACTIVE",
    "PUP": "PUP",
    "NFI": "NFI",
    "IR": "IR",
}

#: When one report is at least this many hours fresher than another, freshness
#: beats severity in the merge — this is what lets a *cleared* player (fresh
#: healthy report) escape a stale OUT from an older pull. Reports fetched in
#: the same warm pass fall inside the window, where most-severe wins.
FRESHNESS_WINDOW_HOURS = 6.0

#: Raw status spelling (lowercased) → normalized code. Covers Yahoo's codes
#: (``Q``/``D``/``O``/``IR``/``IR-R``/``PUP-P``/``NFI-R``/``SUSP``/``NA``) and
#: Sleeper's words (``Questionable``/``Doubtful``/``Out``/``IR``/``PUP``/
#: ``Sus``/``COV``/``DNR``/``NA``). Unknown spellings fall through the prefix
#: pass below, then default to healthy — an unknown code must never invent a
#: discount.
_STATUS_ALIASES: dict[str, str] = {
    "": STATUS_HEALTHY,
    "active": STATUS_HEALTHY,
    "healthy": STATUS_HEALTHY,
    "p": STATUS_HEALTHY,  # probable — no meaningful availability signal
    "probable": STATUS_HEALTHY,
    "q": "Q",
    "questionable": "Q",
    "gtd": "Q",  # game-time decision
    "dtd": "Q",  # day-to-day
    "d": "D",
    "doubtful": "D",
    "o": "O",
    "out": "O",
    "cov": "O",  # Sleeper COVID list — out, duration unknown
    "na": "NA",
    "n/a": "NA",
    "dnr": "NA",  # did not report
    "sus": "SUS",
    "susp": "SUS",
    "suspended": "SUS",
    "ir": "IR",
    "pup": "PUP",
    "nfi": "NFI",
}

#: Prefixes for Yahoo's suffixed reserve codes (``IR-R``, ``PUP-P``, ``NFI-R``…).
_STATUS_PREFIXES: tuple[tuple[str, str], ...] = (
    ("ir", "IR"),
    ("pup", "PUP"),
    ("nfi", "NFI"),
    ("sus", "SUS"),
)


def normalize_status(raw: str | None) -> str:
    """Normalize any source's injury-status spelling to one small code set.

    Yahoo ``"Q"``, Sleeper ``"Questionable"`` and a lowercase ``"questionable"``
    all become ``"Q"``. Unknown spellings normalize to healthy — a code we
    can't read must never manufacture a discount.
    """
    text = str(raw or "").strip().lower()
    if text in _STATUS_ALIASES:
        return _STATUS_ALIASES[text]
    for prefix, code in _STATUS_PREFIXES:
        if text.startswith(prefix):
            return code
    return STATUS_HEALTHY


@dataclass(slots=True)
class InjuryReport:
    """One source's current injury designation for one player.

    ``status`` is the normalized code (:func:`normalize_status`); ``raw_status``
    keeps the source's own spelling; ``detail`` is the body part / note when the
    source has one (Sleeper's ``injury_body_part``, Yahoo's ``injury_note``);
    ``practice`` is practice participation when known. ``fetched_at`` is an ISO
    UTC timestamp — the vintage the merge and the founder both reason from.
    """

    source: str
    status: str = STATUS_HEALTHY
    raw_status: str = ""
    detail: str = ""
    practice: str = ""
    fetched_at: str = ""

    @property
    def severity(self) -> int:
        """The merge-ordering severity of this report's status."""
        return STATUS_SEVERITY.get(self.status, 0)

    @property
    def label(self) -> str:
        """Human-readable status name (``"QUESTIONABLE"``), empty if healthy."""
        return STATUS_LABELS.get(self.status, self.status)


def _parse_when(stamp: str) -> datetime | None:
    """Parse an ISO timestamp, tolerating missing/invalid values as unknown."""
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def merge_reports(reports: Iterable[InjuryReport]) -> InjuryReport | None:
    """Merge multi-source reports for one player: fresh beats stale, severe beats mild.

    The rule, in order:

    1. If one report is **more than** :data:`FRESHNESS_WINDOW_HOURS` fresher
       than every other, it wins outright — a player cleared this morning must
       beat last night's OUT, and vice versa.
    2. Otherwise (same warm pass / comparable vintage / unparseable stamps) the
       **most severe** designation wins.
    3. Ties break to the most recent, then to input order (stable).

    Returns ``None`` for an empty input. The winning report keeps its source
    label — "labelled by source" survives the merge.
    """
    pool = list(reports)
    if not pool:
        return None

    stamped = [(r, _parse_when(r.fetched_at)) for r in pool]
    dated = [(r, when) for r, when in stamped if when is not None]
    if len(dated) > 1:
        newest_report, newest_when = max(dated, key=lambda rw: rw[1])
        window = FRESHNESS_WINDOW_HOURS * 3600.0
        if all(
            (newest_when - when).total_seconds() > window
            for r, when in dated
            if r is not newest_report
        ):
            return newest_report

    def sort_key(rw: tuple[InjuryReport, datetime | None]) -> tuple:
        report, when = rw
        return (report.severity, when or datetime.min.replace(tzinfo=timezone.utc))

    return max(stamped, key=sort_key)[0]


# --------------------------------------------------------------------------- #
# Sleeper — the free, frequently-updated live status source (framework §2.5)
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SleeperInjury:
    """One Sleeper players-blob row reduced to its ids + injury report.

    Carries every id the blob exposes so the caller can resolve to a canonical
    player through whichever spoke it has (§3.1 — sleeper, gsis, then yahoo).
    """

    sleeper_id: str
    gsis_id: str = ""
    yahoo_id: str = ""
    name: str = ""
    position: str = ""
    report: InjuryReport = field(default_factory=lambda: InjuryReport(source="sleeper"))


def _utc_now_iso() -> str:
    """Current UTC time as a second-resolution ISO string (default clock)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sleeper_injuries(
    players_blob: Mapping[str, Mapping[str, object]],
    *,
    fetched_at: str = "",
) -> list[SleeperInjury]:
    """Reduce Sleeper's ``players/nfl`` blob to per-player injury reports.

    Emits a report for **every active-roster row, healthy included** — a fresh
    healthy report is what clears a stale designation in the merge, so "no
    injury" is data, not absence. Rows without a team (long-retired blob
    entries) are skipped to keep the result draft-relevant.
    """
    stamp = fetched_at or _utc_now_iso()
    out: list[SleeperInjury] = []
    for sleeper_id, row in players_blob.items():
        if not isinstance(row, Mapping):
            continue
        team = str(row.get("team") or "")
        if not team:
            continue
        raw_status = str(row.get("injury_status") or "")
        out.append(
            SleeperInjury(
                sleeper_id=str(sleeper_id),
                gsis_id=str(row.get("gsis_id") or "").strip(),
                yahoo_id=str(row.get("yahoo_id") or "").strip(),
                name=str(row.get("full_name") or ""),
                position=normalize_position(str(row.get("position") or "")),
                report=InjuryReport(
                    source="sleeper",
                    status=normalize_status(raw_status),
                    raw_status=raw_status,
                    detail=str(row.get("injury_body_part") or ""),
                    practice=str(row.get("practice_participation") or ""),
                    fetched_at=stamp,
                ),
            )
        )
    return out


class SleeperStatusSource:
    """Live injury statuses from Sleeper, resolved to canonical ids.

    The live loop's status re-check target: one keyless GET returns every
    player's current ``injury_status`` at once. Resolution runs through the
    id maps the caller builds from its player universe (sleeper → gsis →
    yahoo spokes, §3.1); unresolvable rows are dropped — an id we can't name
    can't move the board.

    Politeness: the blob is large, so callers re-check on a slow cadence (the
    loop defaults to ~2 minutes), not per 2.5-second draft poll.
    """

    name = "sleeper"

    def __init__(
        self,
        sleeper: SleeperSource | None = None,
        *,
        by_sleeper_id: Mapping[str, str] | None = None,
        by_gsis_id: Mapping[str, str] | None = None,
        by_yahoo_id: Mapping[str, str] | None = None,
        now: Callable[[], str] | None = None,
    ) -> None:
        self._sleeper = sleeper or SleeperSource()
        self._by_sleeper = dict(by_sleeper_id or {})
        self._by_gsis = dict(by_gsis_id or {})
        self._by_yahoo = dict(by_yahoo_id or {})
        self._now = now or _utc_now_iso

    @classmethod
    def for_players(
        cls, players: Iterable[object], sleeper: SleeperSource | None = None, **kwargs: object
    ) -> "SleeperStatusSource":
        """Build the id maps from canonical players (``store.canonical_players()``)."""
        by_sleeper: dict[str, str] = {}
        by_gsis: dict[str, str] = {}
        by_yahoo: dict[str, str] = {}
        for p in players:
            ids = getattr(p, "ids", None)
            cid = getattr(p, "canonical_id", None)
            if ids is None or cid is None:
                continue
            if getattr(ids, "sleeper_id", None):
                by_sleeper[str(ids.sleeper_id)] = cid
            if getattr(ids, "gsis_id", None):
                by_gsis[str(ids.gsis_id)] = cid
            if getattr(ids, "yahoo_id", None):
                by_yahoo[str(ids.yahoo_id)] = cid
        return cls(
            sleeper,
            by_sleeper_id=by_sleeper,
            by_gsis_id=by_gsis,
            by_yahoo_id=by_yahoo,
            **kwargs,  # type: ignore[arg-type]
        )

    def fetch(self) -> dict[str, InjuryReport]:
        """One live pull: ``{canonical_id: report}`` for every resolvable player."""
        blob = self._sleeper.players()
        out: dict[str, InjuryReport] = {}
        for row in sleeper_injuries(blob, fetched_at=self._now()):
            cid = (
                self._by_sleeper.get(row.sleeper_id)
                or self._by_gsis.get(row.gsis_id)
                or self._by_yahoo.get(row.yahoo_id)
            )
            if cid:
                out[cid] = row.report
        return out


# --------------------------------------------------------------------------- #
# Durability — games-missed history + injury-report history (the risk signal)
# --------------------------------------------------------------------------- #

#: Risk categories, mildest → most severe. ``ELEVATED`` is the chronic tier
#: (a large fraction of recent seasons missed).
RISK_LOW = "low"
RISK_MODERATE = "moderate"
RISK_HIGH = "high"
RISK_ELEVATED = "elevated"
RISK_ORDER: tuple[str, ...] = (RISK_LOW, RISK_MODERATE, RISK_HIGH, RISK_ELEVATED)

#: The honesty label stamped into every durability cache file and store row.
DURABILITY_NOTE = (
    "model estimate (nflverse-based): recency-weighted games missed per season "
    "+ injury-report history. Re-injury cannot be truly predicted — this is a "
    "risk signal and clamped availability discount, not a probability."
)

#: The most the durability discount can ever shade a player's value (before
#: the injury weight scales it further down). A meaningful nudge, never a
#: wild swing — and deliberately small because the projection layer already
#: regresses projected games halfway toward injury history.
DURABILITY_DISCOUNT_CAP = 0.15

#: Soft-tissue injury keywords (recurrence-prone muscle/tendon injuries). A
#: bounded bump, not a diagnosis.
_SOFT_TISSUE_KEYWORDS: tuple[str, ...] = (
    "hamstring",
    "groin",
    "calf",
    "quad",
    "hip",
    "oblique",
    "pectoral",
    "soft tissue",
)

#: Extra discount per distinct soft-tissue (season, body-part) pairing, and the
#: cap on how many count. Small on purpose: a soft-tissue history nudges the
#: needle, it doesn't define the player.
_SOFT_TISSUE_STEP = 0.01
_SOFT_TISSUE_MAX = 3

#: Risk-flag thresholds on the recency-weighted average games missed / season.
_RISK_THRESHOLDS: tuple[tuple[float, str], ...] = (
    (1.5, RISK_LOW),
    (3.5, RISK_MODERATE),
    (6.0, RISK_HIGH),
)

#: NFL regular season length (2021+) — the games baseline missed games count from.
_FULL_SEASON_GAMES = 17.0

#: Positions durability profiles cover (the projectable ones — K/DEF carry no
#: nflverse weekly rows, same limitation the projection model documents).
_DURABLE_POSITIONS = frozenset({"QB", "RB", "WR", "TE"})


def durability_discount(avg_missed: float, soft_tissue: int = 0) -> float:
    """The clamped availability discount for a games-missed history.

    ``avg_missed / 17`` is simply the fraction of a season the player has
    historically missed (recency-weighted); each distinct soft-tissue entry
    adds :data:`_SOFT_TISSUE_STEP` up to :data:`_SOFT_TISSUE_MAX`. The result
    is clamped to :data:`DURABILITY_DISCOUNT_CAP` and is monotonic in games
    missed — more missed football never reads as less risk.
    """
    base = max(0.0, avg_missed) / _FULL_SEASON_GAMES
    bump = _SOFT_TISSUE_STEP * min(max(0, soft_tissue), _SOFT_TISSUE_MAX)
    return round(min(DURABILITY_DISCOUNT_CAP, base + bump), 3)


def risk_flag(avg_missed: float) -> str:
    """Categorical risk from average games missed per season (see thresholds)."""
    for threshold, flag in _RISK_THRESHOLDS:
        if avg_missed < threshold:
            return flag
    return RISK_ELEVATED


@dataclass(slots=True)
class DurabilityProfile:
    """One player's transparent durability signal — facts first, then the model.

    Attributes:
        canonical_id: The hub id (gsis).
        name / position: Display metadata from the most recent season seen.
        games: ``{season: games played}`` — the checkable facts.
        avg_missed: Recency-weighted average games missed per season, over the
            seasons the player actually appeared in.
        seasons_seen: How many seasons back the average.
        designations: Injury-report weeks listed Out/Doubtful across the
            history window (0 when the injuries feed was unavailable).
        soft_tissue: Distinct (season, body part) soft-tissue report pairings.
        risk: Categorical flag (:data:`RISK_ORDER`).
        discount: The clamped availability discount (:func:`durability_discount`).
        note: The honesty label (:data:`DURABILITY_NOTE`).
    """

    canonical_id: str
    name: str = ""
    position: str = ""
    games: dict[int, float] = field(default_factory=dict)
    avg_missed: float = 0.0
    seasons_seen: int = 0
    designations: int = 0
    soft_tissue: int = 0
    risk: str = RISK_LOW
    discount: float = 0.0
    note: str = DURABILITY_NOTE

    @property
    def total_missed(self) -> float:
        """Unweighted total games missed across the seasons seen (display)."""
        return sum(
            max(0.0, _FULL_SEASON_GAMES - g) for g in self.games.values() if g > 0
        )


def _records_of(frame: object) -> list[Mapping[str, object]]:
    """Rows from a DataFrame (``to_dict("records")``) or a plain list of dicts."""
    to_dict = getattr(frame, "to_dict", None)
    if callable(to_dict):
        return to_dict("records")
    return list(frame)  # type: ignore[arg-type]


def _num(row: Mapping[str, object], column: str) -> float:
    """A numeric cell, treating missing/NaN/None as 0.0."""
    val = row.get(column)
    if val is None:
        return 0.0
    try:
        out = float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return out if out == out else 0.0  # NaN != NaN


@dataclass(slots=True)
class DurabilitySource:
    """Builds + caches per-player :class:`DurabilityProfile`\\ s from nflverse.

    Same design rules as the projection/schedule sources: the fetch layer is
    injectable (tests run offline against fixtures), and the per-season JSON
    cache makes draft day zero-network after one online :meth:`warm_cache`.
    The injuries feed is a *bonus* input — if it fails (older seasons, asset
    drift), profiles degrade to games-missed only rather than erroring.

    Args:
        nflverse: The fetch layer (``weekly_stats`` + ``injuries``).
        history_seasons: How many completed seasons of history to read.
        season_weights: Recency weights, most recent season first (same shape
            as the projection model's).
        cache_dir: Where per-season durability JSON caches live (git-ignored).
    """

    name: str = "nflverse_durability"
    nflverse: NflverseSource = field(default_factory=NflverseSource)
    history_seasons: int = 3
    season_weights: tuple[float, ...] = (0.6, 0.25, 0.15)
    cache_dir: Path = field(default_factory=lambda: Path(".cache"))

    @property
    def is_live(self) -> bool:
        """True — nflverse needs no key (reachability is per-call)."""
        return True

    def load(self, season: int) -> list[DurabilityProfile]:
        """The season's profiles — from cache when present, else computed live.

        Raises:
            RuntimeError: If no cache exists and the live nflverse pull fails
                (run :meth:`warm_cache` while online, framework §7).
        """
        cached = self._load_cache(season)
        if cached is not None:
            return cached
        try:
            profiles = self._compute(season)
        except Exception as exc:  # network / parquet / schema failures
            raise RuntimeError(
                f"nflverse durability pull failed for season {season} and no "
                f"local cache exists at {self._cache_path(season)}. Run "
                "warm_cache() while online (pre-draft warm cache, framework §7)."
            ) from exc
        self._write_cache(season, profiles)
        return profiles

    def warm_cache(self, season: int) -> list[DurabilityProfile]:
        """Fetch + compute + persist profiles for ``season`` (pre-draft step)."""
        profiles = self._compute(season)
        self._write_cache(season, profiles)
        return profiles

    # -- the computation -------------------------------------------------------

    def _weight_for(self, recency_index: int) -> float:
        """Recency weight for the ``recency_index``-th most recent season."""
        if not self.season_weights:
            return 1.0
        return self.season_weights[min(recency_index, len(self.season_weights) - 1)]

    def _compute(self, season: int) -> list[DurabilityProfile]:
        """Build profiles from weekly games history + the injuries feed."""
        years = [season - offset for offset in range(1, self.history_seasons + 1)]

        # Games actually played per (player, season) — the checkable facts.
        games: dict[str, dict[int, float]] = {}
        meta: dict[str, tuple[int, str, str]] = {}  # gsis -> (season, name, pos)
        for row in _records_of(self.nflverse.weekly_stats(years)):
            if str(row.get("season_type", "REG") or "REG") != "REG":
                continue
            gsis_id = str(row.get("player_id", "") or "")
            position = normalize_position(str(row.get("position", "") or ""))
            if not gsis_id or position not in _DURABLE_POSITIONS:
                continue
            year = int(_num(row, "season"))
            by_year = games.setdefault(gsis_id, {})
            by_year[year] = by_year.get(year, 0.0) + 1.0
            if year >= meta.get(gsis_id, (0, "", ""))[0]:
                name = str(
                    row.get("player_display_name", row.get("player_name", "")) or ""
                )
                meta[gsis_id] = (year, name, position)

        # Injury-report history — designations + soft-tissue mentions. A feed
        # failure degrades to games-missed-only profiles, never an error.
        designations: dict[str, int] = {}
        soft_pairs: dict[str, set[tuple[int, str]]] = {}
        try:
            injury_rows = _records_of(self.nflverse.injuries(years))
        except Exception:
            injury_rows = []
        for row in injury_rows:
            gsis_id = str(row.get("gsis_id", "") or "")
            if not gsis_id:
                continue
            status = normalize_status(str(row.get("report_status", "") or ""))
            if status in ("O", "D"):
                designations[gsis_id] = designations.get(gsis_id, 0) + 1
            primary = str(row.get("report_primary_injury", "") or "").strip().lower()
            if primary and any(k in primary for k in _SOFT_TISSUE_KEYWORDS):
                soft_pairs.setdefault(gsis_id, set()).add(
                    (int(_num(row, "season")), primary)
                )

        profiles: list[DurabilityProfile] = []
        for gsis_id, by_year in games.items():
            missed_w = 0.0
            weight_sum = 0.0
            seasons_seen = 0
            for recency, year in enumerate(years):
                played = by_year.get(year)
                if played is None or played <= 0:
                    continue  # not in the league that season — not "missed"
                w = self._weight_for(recency)
                missed_w += w * max(0.0, _FULL_SEASON_GAMES - min(played, _FULL_SEASON_GAMES))
                weight_sum += w
                seasons_seen += 1
            if weight_sum <= 0:
                continue
            avg_missed = round(missed_w / weight_sum, 2)
            soft = len(soft_pairs.get(gsis_id, ()))
            _, name, position = meta[gsis_id]
            profiles.append(
                DurabilityProfile(
                    canonical_id=gsis_id,
                    name=name,
                    position=position,
                    games={y: g for y, g in sorted(by_year.items())},
                    avg_missed=avg_missed,
                    seasons_seen=seasons_seen,
                    designations=designations.get(gsis_id, 0),
                    soft_tissue=soft,
                    risk=risk_flag(avg_missed),
                    discount=durability_discount(avg_missed, soft),
                )
            )
        profiles.sort(key=lambda p: (-p.avg_missed, p.canonical_id))
        return profiles

    # -- cache (framework §7 "pre-draft warm cache") --------------------------

    def _cache_path(self, season: int) -> Path:
        return self.cache_dir / f"durability_{self.name}_{season}.json"

    def _write_cache(self, season: int, profiles: list[DurabilityProfile]) -> None:
        """Persist profiles as JSON, stamped with vintage + the honesty note."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "source": self.name,
            "note": DURABILITY_NOTE,
            "season": season,
            "history_seasons": [season - o for o in range(1, self.history_seasons + 1)],
            "generated_on": date.today().isoformat(),
            "profiles": [
                {
                    "canonical_id": p.canonical_id,
                    "name": p.name,
                    "position": p.position,
                    "games": {str(y): g for y, g in p.games.items()},
                    "avg_missed": p.avg_missed,
                    "seasons_seen": p.seasons_seen,
                    "designations": p.designations,
                    "soft_tissue": p.soft_tissue,
                    "risk": p.risk,
                    "discount": p.discount,
                }
                for p in profiles
            ],
        }
        self._cache_path(season).write_text(json.dumps(payload), encoding="utf-8")

    def _load_cache(self, season: int) -> list[DurabilityProfile] | None:
        """Load a season's cached profiles, or ``None`` if absent/unreadable."""
        path = self._cache_path(season)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return [
                DurabilityProfile(
                    canonical_id=str(p["canonical_id"]),
                    name=str(p.get("name", "")),
                    position=str(p.get("position", "")),
                    games={int(y): float(g) for y, g in dict(p.get("games", {})).items()},
                    avg_missed=float(p.get("avg_missed", 0.0)),
                    seasons_seen=int(p.get("seasons_seen", 0)),
                    designations=int(p.get("designations", 0)),
                    soft_tissue=int(p.get("soft_tissue", 0)),
                    risk=str(p.get("risk", RISK_LOW)),
                    discount=float(p.get("discount", 0.0)),
                    note=str(payload.get("note", DURABILITY_NOTE)),
                )
                for p in payload.get("profiles", [])
            ]
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None  # corrupt cache -> treat as absent, recompute live
