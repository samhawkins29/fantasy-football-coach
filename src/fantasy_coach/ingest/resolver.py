"""``IdResolver`` — map a Yahoo player onto a canonical id (framework §3.2).

This is *the critical seam* of the data layer: everything downstream joins on the
canonical id this produces. Given a Yahoo
:class:`~fantasy_coach.clients.models.PlayerIdentity` (M2's
``Player.identity()``), the resolver runs the framework's ordered pipeline and
returns a :class:`Resolution` recording *how* it matched, so low-confidence joins
are auditable rather than silent.

Pipeline order (§3.2), highest-priority first — the first stage that hits wins::

    1. manual override      overrides.csv wins over everything (§3.2 step 5)
    2. team defense (DST)    is_defense -> synthetic DST_{TEAM} (§3.2 step 6)
    3. direct yahoo_id       the happy path (§3.2 step 2)
    4. deterministic tuple   unique (clean_name, pos, team) match (§3.2 step 3)
    5. fuzzy (rapidfuzz)      best name match within (pos, team) bucket (step 4)
    6. unmatched             synthetic UNK_{yahoo_id} + review queue (§3.2 fin.)

Defenses are checked *before* the id joins because a Yahoo defense has a
``player_id`` but is never in the DynastyProcess id map — it must map by team
code. Overrides are checked before *that* so a curated fix always wins.

Fuzzy matching uses :mod:`rapidfuzz` (framework §2.2), lazily imported inside the
default scorer so importing this module has no hard dependency on it; the scorer
is injectable for tests and tuning.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

from fantasy_coach.clients.models import PlayerIdentity
from fantasy_coach.ingest.canonical import dst_canonical_id, unresolved_canonical_id
from fantasy_coach.ingest.crosswalk import CrosswalkRow, IdCrosswalk
from fantasy_coach.ingest.names import clean_name, match_key, normalize_position, normalize_team

__all__ = [
    "Resolution",
    "ResolutionReport",
    "IdResolver",
    "DEFAULT_FUZZY_THRESHOLD",
    "FuzzyScorer",
    "load_overrides",
]

#: Minimum rapidfuzz score (0–100) for a fuzzy name match to be *accepted*.
#: Below this a player is left unmatched for manual review (§3.2 step 4). 87 is
#: conservative: within a (position, team) bucket of a handful of candidates,
#: real matches score high (typically 90+); 87 catches "D.J." vs "DJ"-style
#: variants without accepting a different player.
DEFAULT_FUZZY_THRESHOLD = 87.0

# A scorer takes (query_clean_name, candidate_clean_name) -> score in [0, 100].
FuzzyScorer = Callable[[str, str], float]

# Resolution method labels (also the keys of the report's per-method counts).
METHOD_OVERRIDE = "override"
METHOD_DST = "dst"
METHOD_YAHOO_ID = "yahoo_id"
METHOD_DETERMINISTIC = "deterministic"
METHOD_FUZZY = "fuzzy"
METHOD_UNMATCHED = "unmatched"


@dataclass(slots=True)
class Resolution:
    """The outcome of resolving one Yahoo player to a canonical id.

    Attributes:
        yahoo_player_id: The Yahoo id that was resolved.
        canonical_id: The hub id it mapped to (``gsis_id`` / ``DST_x`` / ``UNK_x``).
        method: Which pipeline stage produced the match (see the module constants).
        confidence: ``100.0`` for exact/id/override/dst matches; the rapidfuzz
            score for fuzzy matches; ``0.0`` for unmatched.
        row: The :class:`CrosswalkRow` matched, if any (``None`` for DST and
            unmatched, which have no id-map row).
    """

    yahoo_player_id: str
    canonical_id: str
    method: str
    confidence: float = 100.0
    row: CrosswalkRow | None = None

    @property
    def matched(self) -> bool:
        """True unless the player fell through every stage (``UNK_`` id)."""
        return self.method != METHOD_UNMATCHED

    @property
    def needs_review(self) -> bool:
        """True for fuzzy matches — the review queue the framework calls for."""
        return self.method == METHOD_FUZZY


@dataclass(slots=True)
class ResolutionReport:
    """Aggregate result of :meth:`IdResolver.resolve_all` — coverage + review.

    Surfaces exactly what the framework asks the crosswalk to *report* (§3.2
    step 7 "log unmatched players so coverage improves"): what matched, by which
    method, what needs review (fuzzy), and what didn't match at all.
    """

    resolutions: dict[str, Resolution] = field(default_factory=dict)

    def add(self, res: Resolution) -> None:
        """Record one resolution (keyed by Yahoo player id)."""
        self.resolutions[res.yahoo_player_id] = res

    @property
    def matched(self) -> list[Resolution]:
        """Every resolution that mapped to a real/synthetic canonical id."""
        return [r for r in self.resolutions.values() if r.matched]

    @property
    def unmatched(self) -> list[Resolution]:
        """Players that fell through every stage (the ``UNK_`` review list)."""
        return [r for r in self.resolutions.values() if not r.matched]

    @property
    def needs_review(self) -> list[Resolution]:
        """Fuzzy matches, sorted lowest-confidence first (curate these)."""
        fuzzy = [r for r in self.resolutions.values() if r.needs_review]
        return sorted(fuzzy, key=lambda r: r.confidence)

    def counts_by_method(self) -> dict[str, int]:
        """How many players each pipeline stage resolved."""
        counts: dict[str, int] = {}
        for r in self.resolutions.values():
            counts[r.method] = counts.get(r.method, 0) + 1
        return counts

    @property
    def match_rate(self) -> float:
        """Fraction of players that matched (0.0–1.0); ``1.0`` if empty."""
        total = len(self.resolutions)
        if total == 0:
            return 1.0
        return len(self.matched) / total

    def summary(self) -> dict[str, object]:
        """A compact dict for logging / the M3 report."""
        return {
            "total": len(self.resolutions),
            "matched": len(self.matched),
            "unmatched": len(self.unmatched),
            "needs_review": len(self.needs_review),
            "match_rate": round(self.match_rate, 4),
            "by_method": self.counts_by_method(),
        }


def _default_fuzzy_scorer() -> FuzzyScorer:
    """Build the default rapidfuzz-backed scorer (lazily imported).

    Uses ``WRatio``, which is robust to token order and partial tokens — a good
    default for "same person, slightly different spelling" within a small,
    already position/team-filtered candidate set.
    """
    from rapidfuzz import fuzz  # noqa: PLC0415  (intentional lazy import)

    def score(a: str, b: str) -> float:
        return float(fuzz.WRatio(a, b))

    return score


class IdResolver:
    """Resolve Yahoo players to canonical ids via the §3.2 pipeline.

    Args:
        crosswalk: The master :class:`IdCrosswalk` (from ``import_ids()`` +
            optional Sleeper gap-fill).
        overrides: Manual ``{yahoo_player_id: gsis_id}`` map — the highest
            priority stage (§3.2 step 5). Curate rookie/trade edge cases here.
        fuzzy_threshold: Minimum score to accept a fuzzy match.
        fuzzy_scorer: ``(name_a, name_b) -> score`` in [0, 100]. Defaults to a
            lazily-imported rapidfuzz ``WRatio``. Injectable so tests need no
            rapidfuzz and can force exact-only behaviour.
        allow_team_fuzzy_fallback: When a ``(position, team)`` bucket is empty
            (e.g. Yahoo lists a player at a position nflverse doesn't), also try
            fuzzy across the whole team. Off by default — conservative.
    """

    def __init__(
        self,
        crosswalk: IdCrosswalk,
        *,
        overrides: dict[str, str] | None = None,
        fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
        fuzzy_scorer: FuzzyScorer | None = None,
        allow_team_fuzzy_fallback: bool = False,
    ) -> None:
        self._crosswalk = crosswalk
        self._overrides = dict(overrides or {})
        self._threshold = float(fuzzy_threshold)
        self._scorer = fuzzy_scorer
        self._allow_team_fuzzy = allow_team_fuzzy_fallback

    @property
    def crosswalk(self) -> IdCrosswalk:
        """The crosswalk this resolver matches against."""
        return self._crosswalk

    def _get_scorer(self) -> FuzzyScorer:
        """Return the fuzzy scorer, building the rapidfuzz default on first use."""
        if self._scorer is None:
            self._scorer = _default_fuzzy_scorer()
        return self._scorer

    # -- single resolve ------------------------------------------------------

    def resolve(self, identity: PlayerIdentity) -> Resolution:
        """Resolve one :class:`PlayerIdentity` to a :class:`Resolution` (§3.2).

        Runs the ordered pipeline and returns on the first stage that matches;
        never raises — an unresolvable player yields a ``METHOD_UNMATCHED``
        resolution with a synthetic ``UNK_`` id so callers can still carry it.
        """
        yid = identity.yahoo_player_id

        # 1. Manual override — wins over everything (§3.2 step 5).
        if yid and yid in self._overrides:
            gsis = self._overrides[yid]
            row = self._crosswalk.by_source_id("gsis_id", gsis)
            return Resolution(yid, gsis, METHOD_OVERRIDE, 100.0, row)

        # 2. Team defense — map by team code, never by id (§3.2 step 6).
        if identity.is_defense:
            team = normalize_team(identity.team_abbr) or identity.team_abbr.strip().upper()
            return Resolution(yid, dst_canonical_id(team), METHOD_DST, 100.0, None)

        # 3. Direct Yahoo id join — the happy path (§3.2 step 2).
        row = self._crosswalk.by_yahoo_id(yid)
        if row is not None and row.gsis_id:
            return Resolution(yid, row.gsis_id, METHOD_YAHOO_ID, 100.0, row)

        # 4. Deterministic (clean_name, position, team) tuple (§3.2 step 3).
        key = match_key(identity.full_name, identity.position, identity.team_abbr)
        row = self._crosswalk.by_match_key(key)
        if row is not None and row.gsis_id:
            return Resolution(yid, row.gsis_id, METHOD_DETERMINISTIC, 100.0, row)

        # 5. Fuzzy name match within the (position, team) bucket (§3.2 step 4).
        fuzzy = self._fuzzy_match(identity)
        if fuzzy is not None:
            row, score = fuzzy
            return Resolution(yid, row.gsis_id or unresolved_canonical_id(yid),
                              METHOD_FUZZY, score, row)

        # 6. Unmatched — synthetic id + review queue (§3.2 finale).
        return Resolution(yid, unresolved_canonical_id(yid), METHOD_UNMATCHED, 0.0, None)

    def _fuzzy_match(self, identity: PlayerIdentity) -> tuple[CrosswalkRow, float] | None:
        """Best above-threshold fuzzy match for ``identity``, or ``None``.

        Scores the player's clean name against each candidate in its
        ``(position, team)`` bucket (optionally the whole team) and returns the
        single best if it clears the threshold. Only candidates with a real
        ``gsis_id`` are eligible — a fuzzy hit onto a row that itself lacks a hub
        id would be useless.
        """
        query = clean_name(identity.full_name)
        if not query:
            return None
        position = normalize_position(identity.position)
        team = normalize_team(identity.team_abbr)

        candidates = self._crosswalk.candidates_in_bucket(position, team)
        if not candidates and self._allow_team_fuzzy and team:
            candidates = self._crosswalk.candidates_by_team(team)
        candidates = [c for c in candidates if c.gsis_id]
        if not candidates:
            return None

        scorer = self._get_scorer()
        best_row: CrosswalkRow | None = None
        best_score = -1.0
        for cand in candidates:
            score = scorer(query, cand.clean_name)
            if score > best_score:
                best_score = score
                best_row = cand
        if best_row is not None and best_score >= self._threshold:
            return best_row, best_score
        return None

    # -- batch resolve -------------------------------------------------------

    def resolve_all(self, identities: Iterable[PlayerIdentity]) -> ResolutionReport:
        """Resolve many identities into a :class:`ResolutionReport` (§3.2 step 7).

        The report aggregates coverage (match rate, per-method counts), the
        fuzzy review queue, and the unmatched list — the feedback loop that
        drives override curation over the season.
        """
        report = ResolutionReport()
        for identity in identities:
            report.add(self.resolve(identity))
        return report

    # -- override management -------------------------------------------------

    def add_override(self, yahoo_player_id: str, gsis_id: str) -> None:
        """Add / update a manual override (a curated fix; §3.2 step 5)."""
        self._overrides[yahoo_player_id] = gsis_id

    @property
    def overrides(self) -> dict[str, str]:
        """The current override table (read-only copy)."""
        return dict(self._overrides)


def load_overrides(rows: Sequence[dict[str, str]]) -> dict[str, str]:
    """Build an overrides map from records with ``yahoo_id`` + ``gsis_id`` keys.

    Mirrors an ``overrides.csv`` hand-curated table (§3.2 step 5) without
    binding to a file format — pass rows parsed from CSV/JSON/anywhere.
    """
    out: dict[str, str] = {}
    for rec in rows:
        yid = str(rec.get("yahoo_id") or rec.get("yahoo_player_id") or "").strip()
        gsis = str(rec.get("gsis_id") or "").strip()
        if yid and gsis:
            out[yid] = gsis
    return out
