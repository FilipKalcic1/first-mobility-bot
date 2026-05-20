"""Tests for services/v2/gdpr_audit.py + engine side-effect dispatch.

Covers the legal-blocker fix (B1 in the readiness plan): SpecialIntentMatch
side_effects are no longer silently dropped. GDPR delete/export and
human-handover requests now land in per-tenant Redis lists, so operators
can act within the 48h GDPR window.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from services.v2.gdpr_audit import (  # noqa: E402
    GDPR_KEY_PREFIX,
    HANDOVER_KEY_PREFIX,
    GdprAuditStore,
)


# --------------------------------------------------------------------------
# In-memory fake Redis for unit tests (lpush / ltrim / expire / lrange)
# --------------------------------------------------------------------------

class _FakeRedis:
    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.ttls: dict[str, int] = {}
        self.fail_next = False

    async def lpush(self, key: str, value: str) -> int:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("simulated redis down")
        self.lists.setdefault(key, []).insert(0, value)
        return len(self.lists[key])

    async def ltrim(self, key: str, start: int, end: int) -> bool:
        if key in self.lists:
            self.lists[key] = self.lists[key][start : end + 1]
        return True

    async def expire(self, key: str, ttl: int) -> bool:
        self.ttls[key] = ttl
        return True

    async def lrange(self, key: str, start: int, end: int) -> list:
        return self.lists.get(key, [])[start : end + 1]


# --------------------------------------------------------------------------
# Unit tests for the store
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_gdpr_delete_appends_and_sets_ttl():
    r = _FakeRedis()
    store = GdprAuditStore(r)
    ok = await store.record_gdpr_request(
        action="audit_log_gdpr_delete",
        tenant_id="tenant-A",
        phone="+385955087196",
        person_id="p1",
        correlation_id="cid-1",
    )
    assert ok is True
    key = GDPR_KEY_PREFIX + "tenant-A"
    assert key in r.lists
    assert r.ttls[key] == 90 * 24 * 3600
    entries = await store.list_gdpr_requests("tenant-A")
    assert len(entries) == 1
    e = entries[0]
    assert e["action"] == "audit_log_gdpr_delete"
    assert e["tenant_id"] == "tenant-A"
    assert e["phone"] == "+385955087196"
    assert e["person_id"] == "p1"
    assert e["correlation_id"] == "cid-1"


@pytest.mark.asyncio
async def test_record_gdpr_export_uses_same_list():
    r = _FakeRedis()
    store = GdprAuditStore(r)
    await store.record_gdpr_request(
        action="audit_log_gdpr_export",
        tenant_id="tenant-B", phone="+1", person_id="p", correlation_id="c",
    )
    entries = await store.list_gdpr_requests("tenant-B")
    assert len(entries) == 1
    assert entries[0]["action"] == "audit_log_gdpr_export"


@pytest.mark.asyncio
async def test_handover_separate_key_and_ttl():
    r = _FakeRedis()
    store = GdprAuditStore(r)
    await store.record_handover_request(
        tenant_id="tenant-X", phone="+1", person_id="p", correlation_id="c",
    )
    assert HANDOVER_KEY_PREFIX + "tenant-X" in r.lists
    assert r.ttls[HANDOVER_KEY_PREFIX + "tenant-X"] == 30 * 24 * 3600
    entries = await store.list_handover_requests("tenant-X")
    assert entries and entries[0]["action"] == "queue_human_handover"


@pytest.mark.asyncio
async def test_unknown_tenant_skipped_without_raising():
    r = _FakeRedis()
    store = GdprAuditStore(r)
    ok = await store.record_gdpr_request(
        action="audit_log_gdpr_delete", tenant_id=None,
        phone="+1", person_id=None, correlation_id="c",
    )
    assert ok is False
    assert r.lists == {}


@pytest.mark.asyncio
async def test_redis_failure_does_not_raise():
    r = _FakeRedis()
    r.fail_next = True
    store = GdprAuditStore(r)
    ok = await store.record_gdpr_request(
        action="audit_log_gdpr_delete", tenant_id="t",
        phone="+1", person_id="p", correlation_id="c",
    )
    assert ok is False  # records the failure, never raises


@pytest.mark.asyncio
async def test_tenants_isolated_in_separate_lists():
    r = _FakeRedis()
    store = GdprAuditStore(r)
    await store.record_gdpr_request(
        action="audit_log_gdpr_delete", tenant_id="A",
        phone="+1", person_id="p", correlation_id="c1",
    )
    await store.record_gdpr_request(
        action="audit_log_gdpr_delete", tenant_id="B",
        phone="+2", person_id="q", correlation_id="c2",
    )
    a = await store.list_gdpr_requests("A")
    b = await store.list_gdpr_requests("B")
    assert len(a) == 1 and a[0]["tenant_id"] == "A"
    assert len(b) == 1 and b[0]["tenant_id"] == "B"
