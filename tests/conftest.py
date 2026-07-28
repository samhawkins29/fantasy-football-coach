"""Shared pytest fixtures and helpers.

Everything here is offline: HTTP is faked with ``httpx.MockTransport`` so no
test ever touches the network (framework requirement for M1).
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest

from fantasy_coach.auth.token_store import Token, TokenStore


@pytest.fixture
def token_path(tmp_path: Path) -> Path:
    """A throwaway path for a token file inside pytest's tmp dir."""
    return tmp_path / ".tokens.json"


@pytest.fixture
def token_store(token_path: Path) -> TokenStore:
    """A TokenStore pointed at a throwaway path."""
    return TokenStore(token_path)


@pytest.fixture
def fresh_token() -> Token:
    """A token that is valid for another hour."""
    return Token(
        access_token="access-abc",
        refresh_token="refresh-xyz",
        expires_at=time.time() + 3600,
        token_type="bearer",
        xoauth_yahoo_guid="GUID123",
        scope="fspt-r",
    )


def make_mock_client(handler) -> httpx.Client:
    """Build an ``httpx.Client`` whose requests are served by ``handler``.

    Args:
        handler: A callable ``(httpx.Request) -> httpx.Response``.
    """
    return httpx.Client(transport=httpx.MockTransport(handler))
