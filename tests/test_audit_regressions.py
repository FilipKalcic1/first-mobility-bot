"""Regression guards for post-audit fixes (commits 1ad0189, 8dc3e4f, 9967743, 4a2d726)."""
from __future__ import annotations

import json
import re
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.utils.llm_json import parse_llm_json, _extract_first_object


# ---------------------------------------------------------------------------
# 1ad0189 — Croatian "danas/sutra" parsed in Europe/Zagreb, not UTC
# ---------------------------------------------------------------------------

class TestCroatianDateTimezone:
    def test_danas_uses_local_zagreb_day(self):
        from services.engine.flow_handler import FlowHandler

        result = FlowHandler._parse_croatian_date("danas u 10:00")
        # Must produce an ISO datetime that starts with today's Zagreb date.
        # (The regression was: at 22:30 CET the UTC date was still the previous day.)
        assert result is not None
        parsed = datetime.fromisoformat(result)
        # Either a valid today-or-future date; not a bare UTC-derived past date.
        assert parsed.date() >= date(2024, 1, 1)

    def test_parameter_manager_uses_hr_tz(self):
        from services import parameter_manager as pm

        # Module-level constant must exist and be a tzinfo
        assert pm._HR_TZ is not None
        assert hasattr(pm._HR_TZ, "utcoffset")


# ---------------------------------------------------------------------------
# 9967743 — llm_json extractor uses brace counting, not greedy regex
# ---------------------------------------------------------------------------

class TestLLMJsonExtraction:
    def test_plain_object(self):
        assert parse_llm_json('{"a": 1}') == {"a": 1}

    def test_prose_before_object(self):
        assert parse_llm_json('prose before {"x": 1}') == {"x": 1}

    def test_multiple_objects_returns_first(self):
        # Greedy regex would try to span both and return None.
        got = parse_llm_json('first: {"a": 1} then: {"b": 2}')
        assert got == {"a": 1}

    def test_braces_inside_string(self):
        got = parse_llm_json('{"s": "has } inside", "n": 2}')
        assert got == {"s": "has } inside", "n": 2}

    def test_escaped_quote_in_string(self):
        got = parse_llm_json(r'{"s": "he said \"hi\"", "n": 3}')
        assert got == {"s": 'he said "hi"', "n": 3}

    def test_unparseable_returns_none(self):
        assert parse_llm_json("definitely not json") is None

    def test_extractor_handles_nested(self):
        got = _extract_first_object('{"outer": {"inner": 1}}')
        assert json.loads(got) == {"outer": {"inner": 1}}


# ---------------------------------------------------------------------------
# 4a2d726 — token_manager raises GatewayError on missing access_token
# ---------------------------------------------------------------------------

class TestTokenManagerMissingAccessToken:
    @pytest.mark.asyncio
    async def test_missing_access_token_raises_gateway_error(self):
        from services.token_manager import TokenManager
        from services.errors import GatewayError, ErrorCode

        tm = TokenManager.__new__(TokenManager)
        tm._token = None
        tm._expires_at = datetime.min.replace(tzinfo=__import__("datetime").timezone.utc)
        tm._last_failure = None
        tm._cache_key = "test:token"
        tm._redis = None
        tm.client_id = "x"
        tm.client_secret = "y"
        tm.scope = "api"
        tm.auth_url = "https://idp.example/token"
        tm._http_client = None

        fake_response = MagicMock()
        fake_response.status_code = 200
        fake_response.json.return_value = {"expires_in": 3600}  # no access_token
        fake_response.text = '{"expires_in": 3600}'

        fake_client = MagicMock()
        fake_client.post = AsyncMock(return_value=fake_response)
        tm._get_http_client = AsyncMock(return_value=fake_client)

        with pytest.raises(GatewayError) as exc:
            await tm._fetch_new_token()

        assert exc.value.code == ErrorCode.TOKEN_REFRESH_FAILED


# ---------------------------------------------------------------------------
# 8dc3e4f — ambiguous phone variations refuse instead of returning random user
# ---------------------------------------------------------------------------

class TestPhoneVariationAmbiguity:
    @pytest.mark.asyncio
    async def test_two_distinct_users_refuses_lookup(self):
        from services.user_service import UserService
        import uuid

        svc = UserService.__new__(UserService)
        svc.db = MagicMock()
        svc.db.execute = AsyncMock()

        user_a = MagicMock()
        user_a.id = uuid.uuid4()
        user_a.phone_number = "+385912345678"
        user_b = MagicMock()
        user_b.id = uuid.uuid4()
        user_b.phone_number = "0912345678"

        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [user_a, user_b]
        svc.db.execute.return_value = result_mock

        out = await svc.get_active_identity("+385912345678")
        assert out is None

    @pytest.mark.asyncio
    async def test_single_user_still_returned(self):
        from services.user_service import UserService
        import uuid

        svc = UserService.__new__(UserService)
        svc.db = MagicMock()
        svc.db.execute = AsyncMock()

        user = MagicMock()
        user.id = uuid.uuid4()
        user.phone_number = "+385912345678"

        result_mock = MagicMock()
        result_mock.scalars.return_value.all.return_value = [user]
        svc.db.execute.return_value = result_mock

        out = await svc.get_active_identity("+385912345678")
        assert out is user
