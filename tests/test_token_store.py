"""Tests for the Token model and TokenStore persistence."""

from __future__ import annotations

import json
import time

import pytest

from fantasy_coach.auth.token_store import Token, TokenStore


# -- Token model ------------------------------------------------------------


def test_seconds_until_expiry_and_is_expired():
    now = 1_000_000.0
    token = Token("a", "r", expires_at=now + 100)
    assert token.seconds_until_expiry(now=now) == 100
    assert token.is_expired(now=now) is False
    # near-expiry via leeway
    assert token.is_expired(leeway=200, now=now) is True
    # past expiry
    assert token.is_expired(now=now + 101) is True


def test_authorization_header_capitalizes_scheme():
    token = Token("tok", "r", expires_at=0, token_type="bearer")
    assert token.authorization_header == "Bearer tok"


def test_from_response_computes_absolute_expiry():
    now = 500.0
    data = {
        "access_token": "acc",
        "refresh_token": "ref",
        "expires_in": 3600,
        "token_type": "bearer",
        "xoauth_yahoo_guid": "G",
    }
    token = Token.from_response(data, now=now)
    assert token.expires_at == now + 3600
    assert token.xoauth_yahoo_guid == "G"


def test_from_response_uses_fallback_refresh_token():
    data = {"access_token": "acc", "expires_in": 3600}  # no refresh_token
    token = Token.from_response(data, fallback_refresh_token="old-refresh")
    assert token.refresh_token == "old-refresh"


def test_from_response_missing_access_token_raises():
    with pytest.raises(KeyError):
        Token.from_response({"refresh_token": "r"})


def test_from_response_missing_refresh_and_no_fallback_raises():
    with pytest.raises(KeyError):
        Token.from_response({"access_token": "a"})


def test_from_dict_ignores_unknown_keys():
    token = Token.from_dict(
        {"access_token": "a", "refresh_token": "r", "expires_at": 1.0, "junk": "x"}
    )
    assert token.access_token == "a"


# -- TokenStore -------------------------------------------------------------


def test_save_then_load_roundtrip(token_store, fresh_token):
    assert token_store.exists() is False
    assert token_store.load() is None

    token_store.save(fresh_token)
    assert token_store.exists() is True

    loaded = token_store.load()
    assert loaded == fresh_token


def test_saved_file_is_valid_json_with_expected_fields(token_store, fresh_token):
    token_store.save(fresh_token)
    raw = json.loads(token_store.path.read_text(encoding="utf-8"))
    assert raw["access_token"] == "access-abc"
    assert raw["refresh_token"] == "refresh-xyz"
    assert "expires_at" in raw


def test_save_is_atomic_no_leftover_tmp(token_store, fresh_token):
    token_store.save(fresh_token)
    tmp = token_store.path.with_name(token_store.path.name + ".tmp")
    assert not tmp.exists()


def test_clear_removes_file(token_store, fresh_token):
    token_store.save(fresh_token)
    assert token_store.clear() is True
    assert token_store.exists() is False
    # clearing again is a no-op returning False
    assert token_store.clear() is False


def test_load_invalid_json_raises_valueerror(token_store):
    token_store.path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        token_store.load()


def test_save_overwrites_existing(token_store, fresh_token):
    token_store.save(fresh_token)
    newer = Token("new-access", "new-refresh", expires_at=time.time() + 10)
    token_store.save(newer)
    assert token_store.load() == newer
