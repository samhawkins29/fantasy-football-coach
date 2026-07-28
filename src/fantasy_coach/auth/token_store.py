"""Token model + local token store.

The :class:`Token` dataclass carries the Yahoo access/refresh pair plus an
absolute expiry (unix epoch seconds), so expiry checks never depend on when the
token was *loaded*. :class:`TokenStore` persists it to a git-ignored JSON file
with restrictive permissions.

No network I/O happens in this module.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

# Fields of the raw Yahoo token response we care to keep.
_KNOWN_FIELDS = {
    "access_token",
    "refresh_token",
    "expires_at",
    "token_type",
    "xoauth_yahoo_guid",
    "scope",
}


@dataclass(slots=True)
class Token:
    """An OAuth 2.0 token pair with an absolute expiry.

    Attributes:
        access_token: Short-lived bearer token (~1 hour on Yahoo).
        refresh_token: Long-lived token used to mint new access tokens.
        expires_at: Absolute expiry as unix epoch seconds.
        token_type: Almost always ``"bearer"``.
        xoauth_yahoo_guid: Yahoo's opaque user id, returned with the token.
        scope: Granted scope string, if the provider returned one.
    """

    access_token: str
    refresh_token: str
    expires_at: float
    token_type: str = "bearer"
    xoauth_yahoo_guid: str | None = None
    scope: str | None = None

    # -- derived state -------------------------------------------------------

    def seconds_until_expiry(self, *, now: float | None = None) -> float:
        """Seconds until expiry (negative if already expired)."""
        now = time.time() if now is None else now
        return self.expires_at - now

    def is_expired(self, *, leeway: float = 0.0, now: float | None = None) -> bool:
        """Whether the access token is expired.

        Args:
            leeway: Treat the token as expired this many seconds *before* the
                real expiry. Use a positive leeway to refresh pre-emptively.
            now: Override the current time (seconds since epoch); for testing.
        """
        return self.seconds_until_expiry(now=now) <= leeway

    @property
    def authorization_header(self) -> str:
        """The ``Authorization`` header value for this token."""
        scheme = (self.token_type or "bearer").strip().capitalize()
        return f"{scheme} {self.access_token}"

    # -- (de)serialization ---------------------------------------------------

    @classmethod
    def from_response(
        cls,
        data: dict,
        *,
        now: float | None = None,
        fallback_refresh_token: str | None = None,
    ) -> "Token":
        """Build a :class:`Token` from a raw Yahoo token-endpoint response.

        Yahoo returns ``expires_in`` (relative seconds); we convert it to an
        absolute ``expires_at``. On a refresh, Yahoo may omit ``refresh_token``
        — in that case we keep the prior one via ``fallback_refresh_token``.

        Args:
            data: Parsed JSON body from the token endpoint.
            now: Reference "now" in epoch seconds (defaults to ``time.time()``).
            fallback_refresh_token: Refresh token to retain if the response
                does not include a new one.

        Raises:
            KeyError: If no access token and no usable refresh token is present.
        """
        now = time.time() if now is None else now
        expires_in = float(data.get("expires_in", 3600))
        refresh_token = data.get("refresh_token") or fallback_refresh_token
        if not data.get("access_token"):
            raise KeyError("token response missing 'access_token'")
        if not refresh_token:
            raise KeyError("token response missing 'refresh_token' and no fallback given")
        return cls(
            access_token=data["access_token"],
            refresh_token=refresh_token,
            expires_at=now + expires_in,
            token_type=data.get("token_type", "bearer"),
            xoauth_yahoo_guid=data.get("xoauth_yahoo_guid"),
            scope=data.get("scope"),
        )

    def to_dict(self) -> dict:
        """Plain-dict form suitable for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Token":
        """Inverse of :meth:`to_dict`; ignores unknown keys."""
        filtered = {k: v for k, v in data.items() if k in _KNOWN_FIELDS}
        return cls(**filtered)


class TokenStore:
    """Persists a :class:`Token` to a local JSON file (git-ignored).

    The file is written with owner-only permissions (0600) where the OS
    supports it. This is a plaintext-on-disk store guarded by filesystem
    permissions; encryption-at-rest is a deliberate later enhancement (see
    README §Security).
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        """Args: path: Location of the token JSON file (e.g. ``.tokens.json``)."""
        self.path = Path(path)

    def exists(self) -> bool:
        """Whether a token file is present on disk."""
        return self.path.is_file()

    def load(self) -> Token | None:
        """Load and return the stored token, or ``None`` if absent.

        Raises:
            ValueError: If the file exists but is not valid/complete token JSON.
        """
        if not self.path.is_file():
            return None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"token file {self.path} is not valid JSON: {exc}") from exc
        try:
            return Token.from_dict(raw)
        except TypeError as exc:  # missing required fields
            raise ValueError(f"token file {self.path} is missing required fields: {exc}") from exc

    def save(self, token: Token) -> None:
        """Atomically persist ``token`` to disk with 0600 permissions.

        Writes to a temp file in the same directory then replaces the target,
        so a crash mid-write can't corrupt an existing valid token file.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps(token.to_dict(), indent=2, sort_keys=True)
        tmp.write_text(payload, encoding="utf-8")
        self._restrict_permissions(tmp)
        os.replace(tmp, self.path)  # atomic on POSIX and Windows
        self._restrict_permissions(self.path)

    def clear(self) -> bool:
        """Delete the token file if present. Returns True if a file was removed."""
        try:
            self.path.unlink()
            return True
        except FileNotFoundError:
            return False

    @staticmethod
    def _restrict_permissions(path: Path) -> None:
        """Best-effort chmod to owner read/write only.

        On Windows ``chmod`` is largely a no-op for POSIX bits; the file still
        lands in the user's profile and is git-ignored. We swallow errors so a
        restrictive/exotic filesystem can't break token persistence.
        """
        try:
            os.chmod(path, 0o600)
        except (OSError, NotImplementedError):
            pass
