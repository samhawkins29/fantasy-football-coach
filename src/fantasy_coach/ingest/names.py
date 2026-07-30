"""Name / team / position normalization for the id crosswalk (framework §3.2).

Joining fantasy sources on *name* is unreliable — ``Jr./Sr./II/III`` suffixes,
``D.J.`` vs ``DJ``, apostrophes and accents (``Amon-Ra``, ``Ka'imi``), and every
provider spelling team codes differently (Yahoo ``KC``/``Was`` vs nflverse
``KCC``/``WAS``). §3.2 of the framework therefore normalizes a
``(clean_name, position, team)`` tuple *before* any deterministic or fuzzy match.
This module is that normalizer.

Everything here is **pure** — no I/O, no third-party deps — so it is trivially
testable and safe to call in the hot path. The two things the crosswalk needs
are:

* :func:`clean_name` — a stable, comparable key for a player's name.
* :func:`normalize_team` — collapse every provider's team spelling onto one
  canonical set (also what :data:`DST` mapping keys on).

:func:`match_key` bundles them into the tuple the resolver's deterministic stage
buckets on.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = [
    "CANONICAL_TEAMS",
    "TEAM_ALIASES",
    "NAME_SUFFIXES",
    "clean_name",
    "normalize_team",
    "normalize_position",
    "is_defense_position",
    "match_key",
    "MatchKey",
]

# The 32 canonical NFL team codes we normalize *onto*. These are the modern
# 2–3 letter abbreviations; every provider variant maps into this set so a
# Yahoo player and an nflverse player on the same team compare equal.
CANONICAL_TEAMS = frozenset(
    {
        "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE",
        "DAL", "DEN", "DET", "GB", "HOU", "IND", "JAX", "KC",
        "LV", "LAC", "LAR", "MIA", "MIN", "NE", "NO", "NYG",
        "NYJ", "PHI", "PIT", "SF", "SEA", "TB", "TEN", "WAS",
    }
)

# Every alternate spelling we've seen mapped to its canonical code. Covers:
# * nflverse / DynastyProcess forms (``KCC``, ``SFO``, ``GBP``, ``LVR`` …),
# * Yahoo forms (``Was``, ``Jax`` — handled case-insensitively),
# * relocated / historical franchises (``OAK``→``LV``, ``SD``→``LAC``,
#   ``STL``/``SL``→``LAR``), which matter for older nflverse rows.
# Keys are compared uppercased, so only distinct *letters* need listing here.
TEAM_ALIASES = {
    # nflverse 3-letter -> canonical
    "KCC": "KC",
    "SFO": "SF",
    "SF49": "SF",
    "GBP": "GB",
    "TBB": "TB",
    "NOS": "NO",
    "NEP": "NE",
    "LVR": "LV",
    "ARZ": "ARI",
    "BLT": "BAL",
    "CLV": "CLE",
    "HST": "HOU",
    "JAC": "JAX",
    "WFT": "WAS",
    "WSH": "WAS",
    # relocations / historical
    "OAK": "LV",
    "SD": "LAC",
    "SDG": "LAC",
    "STL": "LAR",
    "SL": "LAR",
    "LA": "LAR",  # ambiguous historically; default to the Rams
    # ESPN / misc two-letter oddities
    "GNB": "GB",
    "KAN": "KC",
    "NWE": "NE",
    "NOR": "NO",
    "SFO49": "SF",
    "TAM": "TB",
    "LVRD": "LV",
}

# Generational suffixes stripped from names before comparison (§3.2).
NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v"})

# Positions that denote a *team defense* rather than an individual player.
# These are mapped by team code to a synthetic ``DST_{TEAM}`` id, never through
# the player id map (framework §3.2 step 6). Yahoo uses ``DEF``; other providers
# use ``DST`` / ``D/ST`` / ``D``.
_DEFENSE_POSITIONS = frozenset({"DEF", "DST", "D/ST", "DST/D", "TMDEF"})

# Characters we treat as word separators inside a name (apostrophes are *dropped*
# so ``Ka'imi`` -> ``kaimi``; hyphens/periods become spaces).
_SEPARATORS = re.compile(r"[.\-_/]+")
_APOSTROPHES = re.compile(r"[’'`]")
_NON_ALNUM_SPACE = re.compile(r"[^a-z0-9 ]+")
_WHITESPACE = re.compile(r"\s+")

# Type alias for the deterministic-match tuple.
MatchKey = tuple[str, str, str]


def _strip_accents(text: str) -> str:
    """Fold accented characters to ASCII (``Amon-Ra`` accents, ``José`` …)."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def clean_name(name: str | None) -> str:
    """Normalize a player name into a stable, comparable key.

    The transform (order matters):

    1. unaccent (``Amon-Ra`` → ``Amon-Ra`` without combining marks),
    2. casefold to lower,
    3. drop apostrophes entirely (``Ka'imi`` → ``kaimi``),
    4. turn ``.``/``-``/``/``/``_`` into spaces (``D.J.`` → ``d j``,
       ``Amon-Ra`` → ``amon ra``),
    5. drop any remaining punctuation,
    6. collapse whitespace and strip a trailing generational suffix
       (``jr``/``sr``/``ii``/``iii``/``iv``/``v``).

    Returns an empty string for ``None``/blank input.

    Examples::

        clean_name("Amon-Ra St. Brown")  -> "amon ra st brown"
        clean_name("Michael Pittman Jr.") -> "michael pittman"
        clean_name("D.J. Moore")          -> "d j moore"
        clean_name("Ken Walker III")      -> "ken walker"
    """
    if not name:
        return ""
    text = _strip_accents(name).lower()
    text = _APOSTROPHES.sub("", text)
    text = _SEPARATORS.sub(" ", text)
    text = _NON_ALNUM_SPACE.sub(" ", text)
    text = _WHITESPACE.sub(" ", text).strip()
    if not text:
        return ""
    parts = text.split(" ")
    # Strip a single trailing generational suffix, but never the only token
    # (guards one-word display names / defenses).
    if len(parts) > 1 and parts[-1] in NAME_SUFFIXES:
        parts = parts[:-1]
    return " ".join(parts)


def normalize_team(code: str | None) -> str:
    """Map any provider's team code onto a canonical code (``KCC`` → ``KC``).

    Uppercases, applies the :data:`TEAM_ALIASES` table, and returns the result
    if it is a known canonical team. Unknown / free-agent / empty codes
    (Yahoo uses ``""`` or ``FA`` for unrostered players) normalize to ``""`` so
    they never produce a false team-bucket match.
    """
    if not code:
        return ""
    key = code.strip().upper()
    if not key or key in {"FA", "FA*", "NONE", "N/A", "--"}:
        return ""
    key = TEAM_ALIASES.get(key, key)
    return key if key in CANONICAL_TEAMS else ""


def is_defense_position(position: str | None) -> bool:
    """True when a position string denotes a team defense (maps by team code)."""
    if not position:
        return False
    return position.strip().upper() in _DEFENSE_POSITIONS


def normalize_position(position: str | None) -> str:
    """Normalize a position code, collapsing every team-defense spelling to ``DEF``.

    Individual positions are simply uppercased (``qb`` → ``QB``). Team defenses
    (``DST``/``D/ST``/``D``) collapse to ``DEF`` — Yahoo's spelling — so the
    canonical model has one defense position regardless of source.
    """
    if not position:
        return ""
    if is_defense_position(position):
        return "DEF"
    return position.strip().upper()


def match_key(name: str | None, position: str | None, team: str | None) -> MatchKey:
    """Build the ``(clean_name, position, team)`` tuple the resolver buckets on.

    Position and team are normalized so a Yahoo identity and an nflverse row for
    the same player produce identical keys. This tuple is the deterministic
    fallback's dictionary key and the fuzzy fallback's bucket key (§3.2).
    """
    return (
        clean_name(name),
        normalize_position(position),
        normalize_team(team),
    )
