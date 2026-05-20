"""Tests for services.admin_auth — token verification + per-user audit trail."""
from __future__ import annotations

import importlib
import os

import pytest


@pytest.fixture
def admin_auth(monkeypatch):
    """Clean ADMIN_TOKEN_* env then return freshly loaded module."""
    for k in list(os.environ):
        if k.startswith("ADMIN_TOKEN"):
            monkeypatch.delenv(k, raising=False)
    import services.admin_auth as mod
    importlib.reload(mod)
    return mod


def test_no_tokens_configured_denies_all(admin_auth):
    assert admin_auth.verify_admin_token("anything") is None
    assert admin_auth.verify_admin_token(None) is None
    assert admin_auth.verify_admin_token("") is None


def test_returns_user_label_on_match(admin_auth, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN_1", "secret_filip")
    monkeypatch.setenv("ADMIN_TOKEN_1_USER", "filip.kalcic")
    monkeypatch.setenv("ADMIN_TOKEN_2", "secret_damir")
    monkeypatch.setenv("ADMIN_TOKEN_2_USER", "damir.skrtic")

    assert admin_auth.verify_admin_token("secret_filip") == "filip.kalcic"
    assert admin_auth.verify_admin_token("secret_damir") == "damir.skrtic"


def test_wrong_token_returns_none(admin_auth, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN_1", "secret_filip")
    monkeypatch.setenv("ADMIN_TOKEN_1_USER", "filip.kalcic")

    assert admin_auth.verify_admin_token("wrong") is None
    assert admin_auth.verify_admin_token(None) is None
    assert admin_auth.verify_admin_token("") is None


def test_slot_fallback_label_when_user_unset(admin_auth, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN_3", "lonely_token")
    # No ADMIN_TOKEN_3_USER

    assert admin_auth.verify_admin_token("lonely_token") == "slot_3"


def test_extract_bearer_or_query_token(admin_auth):
    class Req:
        def __init__(self, qp=None, hdr=None):
            self.query_params = qp or {}
            self.headers = hdr or {}

    f = admin_auth.extract_bearer_or_query_token
    assert f(Req()) == ""
    assert f(Req(qp={"token": "abc"})) == "abc"
    assert f(Req(hdr={"authorization": "Bearer abc"})) == "abc"
    assert f(Req(hdr={"authorization": "bearer abc"})) == "abc"
    # Query takes precedence over header (order asserted in implementation)
    assert f(Req(qp={"token": "from_query"}, hdr={"authorization": "Bearer from_hdr"})) == "from_query"
    # Non-bearer schemes ignored
    assert f(Req(hdr={"authorization": "Basic abc"})) == ""


def test_truthy_falsy_contract_preserved_for_existing_callers(admin_auth, monkeypatch):
    """verify_admin_token shifted from bool to Optional[str], but existing
    `if not verify_admin_token(...)` callers in webhook_simple.py rely on
    truthy/falsy. Ensure both poles still work."""
    monkeypatch.setenv("ADMIN_TOKEN_1", "secret")
    monkeypatch.setenv("ADMIN_TOKEN_1_USER", "ada")

    assert bool(admin_auth.verify_admin_token("secret")) is True
    assert bool(admin_auth.verify_admin_token("nope")) is False
    assert bool(admin_auth.verify_admin_token(None)) is False
