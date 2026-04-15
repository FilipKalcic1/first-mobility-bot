"""
Comprehensive Tests for services/ai_orchestrator.py

Tests AIOrchestrator class:
- __init__ (mocked OpenAI client)
- analyze (main method, parses OpenAI response: text, tool_call, error)
- _count_tokens (fallback and tiktoken paths)
- _apply_smart_history (sliding window, summarization)
- _apply_token_budgeting (trimming by MAX_TOOLS_FOR_LLM)
- _calculate_backoff (exponential backoff with jitter)
- _handle_rate_limit / _handle_timeout (retry logic)
- get_token_stats / get_retry_status
- extract_parameters (param extraction via LLM)
- build_system_prompt (prompt construction)
- _extract_entities / _format_entity_context / _summarize_conversation
"""

import json
import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock
from typing import Dict, Any, List


# ---------------------------------------------------------------------------
# Helper: create a mock settings object that satisfies AIOrchestrator.__init__
# ---------------------------------------------------------------------------
def _make_mock_settings():
    s = MagicMock()
    s.AZURE_OPENAI_ENDPOINT = "https://test.openai.azure.com"
    s.AZURE_OPENAI_API_KEY = "test-key"
    s.AZURE_OPENAI_API_VERSION = "2024-02-15"
    s.AZURE_OPENAI_DEPLOYMENT_NAME = "gpt-4"
    s.AI_TEMPERATURE = 0.2
    s.AI_MAX_TOKENS = 1500
    s.MAX_TOOLS_FOR_LLM = 25
    return s


def _make_passthrough_circuit_breaker():
    """Create a mock circuit breaker that passes calls through to the function."""
    cb = MagicMock()
    async def passthrough_call(endpoint_key, func, *args, **kwargs):
        return await func(*args, **kwargs)
    cb.call = AsyncMock(side_effect=passthrough_call)
    return cb


def _make_orchestrator(mock_settings=None):
    """Instantiate AIOrchestrator with all external deps mocked."""
    if mock_settings is None:
        mock_settings = _make_mock_settings()

    with patch("services.ai_orchestrator._get_settings", return_value=mock_settings):
        with patch("services.ai_orchestrator.get_openai_client") as MockGetClient:
            with patch("services.ai_orchestrator.get_llm_circuit_breaker") as MockGetCB:
                mock_client_instance = MagicMock()
                MockGetClient.return_value = mock_client_instance
                MockGetCB.return_value = _make_passthrough_circuit_breaker()
                from services.ai_orchestrator import AIOrchestrator
                orch = AIOrchestrator()
    return orch


def _make_openai_text_response(text="Hello", prompt_tokens=100, completion_tokens=50):
    """Build a mock OpenAI chat completion with a text response."""
    mock_choice = MagicMock()
    mock_choice.message.content = text
    mock_choice.message.tool_calls = None
    mock_choice.finish_reason = "stop"

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage.prompt_tokens = prompt_tokens
    mock_response.usage.completion_tokens = completion_tokens
    return mock_response


def _make_openai_tool_response(
    tool_name="get_MasterData",
    arguments='{"Filter": "PersonId(=)123"}',
    call_id="call_123",
    prompt_tokens=120,
    completion_tokens=30,
):
    """Build a mock OpenAI chat completion with a tool_call response."""
    mock_tool_call = MagicMock()
    mock_tool_call.function.name = tool_name
    mock_tool_call.function.arguments = arguments
    mock_tool_call.id = call_id

    mock_choice = MagicMock()
    mock_choice.message.content = None
    mock_choice.message.tool_calls = [mock_tool_call]
    mock_choice.finish_reason = "tool_calls"

    mock_response = MagicMock()
    mock_response.choices = [mock_choice]
    mock_response.usage.prompt_tokens = prompt_tokens
    mock_response.usage.completion_tokens = completion_tokens
    return mock_response


# ============================================================================
# 1. __init__ tests
# ============================================================================

class TestInit:
    """Tests for AIOrchestrator.__init__."""

    def test_init_creates_client(self):
        """Client is obtained from shared openai_client singleton."""
        ms = _make_mock_settings()
        mock_client = MagicMock()
        with patch("services.ai_orchestrator._get_settings", return_value=ms):
            with patch("services.ai_orchestrator.get_openai_client", return_value=mock_client):
                with patch("services.ai_orchestrator.get_llm_circuit_breaker", return_value=MagicMock()):
                    from services.ai_orchestrator import AIOrchestrator
                    orch = AIOrchestrator()
                    assert orch.client is mock_client

    def test_init_sets_model(self):
        orch = _make_orchestrator()
        assert orch.model == "gpt-4"

    def test_init_zeroes_counters(self):
        orch = _make_orchestrator()
        assert orch._total_prompt_tokens == 0
        assert orch._total_completion_tokens == 0
        assert orch._total_requests == 0
        assert orch._rate_limit_hits == 0

    def test_init_tokenizer_none_when_tiktoken_unavailable(self):
        """When tiktoken import fails, tokenizer is None."""
        ms = _make_mock_settings()
        with patch("services.ai_orchestrator._get_settings", return_value=ms):
            with patch("services.ai_orchestrator.get_openai_client", return_value=MagicMock()):
                with patch("services.ai_orchestrator.get_llm_circuit_breaker", return_value=MagicMock()):
                    with patch("services.ai_orchestrator.tiktoken", None):
                        from services.ai_orchestrator import AIOrchestrator
                        orch = AIOrchestrator()
                        assert orch.tokenizer is None


# ============================================================================
# 2. analyze tests
# ============================================================================

class TestAnalyze:
    """Tests for AIOrchestrator.analyze (main method)."""

    @pytest.mark.asyncio
    async def test_analyze_text_response(self):
        """analyze returns type=text for a normal text completion."""
        orch = _make_orchestrator()
        resp = _make_openai_text_response("Bok!")
        orch.client.chat.completions.create = AsyncMock(return_value=resp)

        result = await orch.analyze(
            messages=[{"role": "user", "content": "Bok"}],
            system_prompt="Be helpful",
        )

        assert result["type"] == "text"
        assert result["content"] == "Bok!"
        assert result["usage"]["prompt_tokens"] == 100
        assert result["usage"]["completion_tokens"] == 50

    @pytest.mark.asyncio
    async def test_analyze_tool_call_response(self):
        """analyze returns type=tool_call when LLM invokes a tool."""
        orch = _make_orchestrator()
        resp = _make_openai_tool_response(
            tool_name="get_Vehicles",
            arguments='{"status": "active"}',
            call_id="call_abc",
        )
        orch.client.chat.completions.create = AsyncMock(return_value=resp)

        tools = [{"type": "function", "function": {"name": "get_Vehicles", "parameters": {}}}]
        result = await orch.analyze(
            messages=[{"role": "user", "content": "Pokaži vozila"}],
            tools=tools,
        )

        assert result["type"] == "tool_call"
        assert result["tool"] == "get_Vehicles"
        assert result["parameters"] == {"status": "active"}
        assert result["tool_call_id"] == "call_abc"

    @pytest.mark.asyncio
    async def test_analyze_tool_call_invalid_json_arguments(self):
        """When tool_call arguments are invalid JSON, parameters default to {}."""
        orch = _make_orchestrator()
        resp = _make_openai_tool_response(arguments="NOT_JSON")
        orch.client.chat.completions.create = AsyncMock(return_value=resp)

        tools = [{"type": "function", "function": {"name": "get_MasterData", "parameters": {}}}]
        result = await orch.analyze(
            messages=[{"role": "user", "content": "test"}],
            tools=tools,
        )

        assert result["type"] == "tool_call"
        assert result["parameters"] == {}

    @pytest.mark.asyncio
    async def test_analyze_empty_choices(self):
        """analyze returns error when response.choices is empty."""
        orch = _make_orchestrator()
        mock_resp = MagicMock()
        mock_resp.choices = []
        mock_resp.usage.prompt_tokens = 10
        mock_resp.usage.completion_tokens = 0
        orch.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        result = await orch.analyze(messages=[{"role": "user", "content": "test"}])
        assert result["type"] == "error"

    @pytest.mark.asyncio
    async def test_analyze_tracks_token_usage(self):
        """Token counters are incremented after a successful call."""
        orch = _make_orchestrator()
        resp = _make_openai_text_response(prompt_tokens=200, completion_tokens=80)
        orch.client.chat.completions.create = AsyncMock(return_value=resp)

        await orch.analyze(messages=[{"role": "user", "content": "test"}])
        assert orch._total_prompt_tokens == 200
        assert orch._total_completion_tokens == 80
        assert orch._total_requests == 1

    @pytest.mark.asyncio
    async def test_analyze_with_tools_uses_auto_tool_choice(self):
        """When tools are provided, tool_choice is set to auto."""
        orch = _make_orchestrator()
        resp = _make_openai_tool_response(tool_name="get_Vehicles")
        orch.client.chat.completions.create = AsyncMock(return_value=resp)

        tools = [{"type": "function", "function": {"name": "get_Vehicles", "parameters": {}}}]
        await orch.analyze(
            messages=[{"role": "user", "content": "test"}],
            tools=tools,
        )

        call_kwargs = orch.client.chat.completions.create.call_args[1]
        assert call_kwargs["tool_choice"] == "auto"

    @pytest.mark.asyncio
    async def test_analyze_no_tools_omits_tool_fields(self):
        """When tools=None, tool-related keys are absent from call args."""
        orch = _make_orchestrator()
        resp = _make_openai_text_response()
        orch.client.chat.completions.create = AsyncMock(return_value=resp)

        await orch.analyze(messages=[{"role": "user", "content": "test"}])
        call_kwargs = orch.client.chat.completions.create.call_args[1]
        assert "tools" not in call_kwargs
        assert "tool_choice" not in call_kwargs

    @pytest.mark.asyncio
    async def test_analyze_system_prompt_prepended(self):
        """system_prompt is prepended to messages when not already present."""
        orch = _make_orchestrator()
        resp = _make_openai_text_response()
        orch.client.chat.completions.create = AsyncMock(return_value=resp)

        await orch.analyze(
            messages=[{"role": "user", "content": "Bok"}],
            system_prompt="Be helpful",
        )

        call_kwargs = orch.client.chat.completions.create.call_args[1]
        msgs = call_kwargs["messages"]
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == "Be helpful"

    @pytest.mark.asyncio
    async def test_analyze_no_duplicate_system_prompt(self):
        """If messages already start with system, no duplicate is added."""
        orch = _make_orchestrator()
        resp = _make_openai_text_response()
        orch.client.chat.completions.create = AsyncMock(return_value=resp)

        await orch.analyze(
            messages=[
                {"role": "system", "content": "Existing prompt"},
                {"role": "user", "content": "Bok"},
            ],
            system_prompt="New prompt",
        )

        call_kwargs = orch.client.chat.completions.create.call_args[1]
        msgs = call_kwargs["messages"]
        system_msgs = [m for m in msgs if m["role"] == "system"]
        # Should only have the original, not a duplicate
        assert len(system_msgs) == 1
        assert system_msgs[0]["content"] == "Existing prompt"

    @pytest.mark.asyncio
    async def test_analyze_content_none_returns_empty_string(self):
        """When message.content is None, result content should be empty string."""
        orch = _make_orchestrator()
        mock_choice = MagicMock()
        mock_choice.message.content = None
        mock_choice.message.tool_calls = None  # explicitly no tool calls
        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_resp.usage.prompt_tokens = 10
        mock_resp.usage.completion_tokens = 5
        orch.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        result = await orch.analyze(messages=[{"role": "user", "content": "test"}])
        assert result["type"] == "text"
        assert result["content"] == ""


# ============================================================================
# 3. Retry / error handling tests
# ============================================================================

class TestRetryAndErrorHandling:
    """Tests for rate-limit, timeout, and generic error handling in analyze."""

    @pytest.mark.asyncio
    async def test_analyze_rate_limit_retries_then_succeeds(self):
        """RateLimitError on first attempt, success on second."""
        from openai import RateLimitError

        orch = _make_orchestrator()
        good_resp = _make_openai_text_response("Recovered")

        rate_err = RateLimitError(
            message="rate limit",
            response=MagicMock(status_code=429, headers={}),
            body=None,
        )

        orch.client.chat.completions.create = AsyncMock(
            side_effect=[rate_err, good_resp]
        )

        with patch("services.ai_orchestrator.asyncio.sleep", new_callable=AsyncMock):
            result = await orch.analyze(messages=[{"role": "user", "content": "test"}])

        assert result["type"] == "text"
        assert result["content"] == "Recovered"
        assert orch._rate_limit_hits == 1

    @pytest.mark.asyncio
    async def test_analyze_rate_limit_exhausts_retries(self):
        """RateLimitError on all attempts returns error message."""
        from openai import RateLimitError

        orch = _make_orchestrator()
        rate_err = RateLimitError(
            message="rate limit",
            response=MagicMock(status_code=429, headers={}),
            body=None,
        )

        orch.client.chat.completions.create = AsyncMock(
            side_effect=[rate_err, rate_err, rate_err]
        )

        with patch("services.ai_orchestrator.asyncio.sleep", new_callable=AsyncMock):
            result = await orch.analyze(messages=[{"role": "user", "content": "test"}])

        assert result["type"] == "error"
        assert "preopterećen" in result["content"]  # RATE_LIMIT_ERROR_MSG

    @pytest.mark.asyncio
    async def test_analyze_timeout_retries_then_succeeds(self):
        """APITimeoutError on first attempt, success on second."""
        from openai import APITimeoutError

        orch = _make_orchestrator()
        good_resp = _make_openai_text_response("Recovered")

        timeout_err = APITimeoutError(request=MagicMock())

        orch.client.chat.completions.create = AsyncMock(
            side_effect=[timeout_err, good_resp]
        )

        with patch("services.ai_orchestrator.asyncio.sleep", new_callable=AsyncMock):
            result = await orch.analyze(messages=[{"role": "user", "content": "test"}])

        assert result["type"] == "text"
        assert result["content"] == "Recovered"

    @pytest.mark.asyncio
    async def test_analyze_timeout_exhausts_retries(self):
        """APITimeoutError on all attempts returns timeout message."""
        from openai import APITimeoutError

        orch = _make_orchestrator()
        timeout_err = APITimeoutError(request=MagicMock())

        orch.client.chat.completions.create = AsyncMock(
            side_effect=[timeout_err, timeout_err, timeout_err]
        )

        with patch("services.ai_orchestrator.asyncio.sleep", new_callable=AsyncMock):
            result = await orch.analyze(messages=[{"role": "user", "content": "test"}])

        assert result["type"] == "error"
        assert "nije odgovorio" in result["content"]  # TIMEOUT_ERROR_MSG

    @pytest.mark.asyncio
    async def test_analyze_api_status_error_429_retries(self):
        """APIStatusError with 429 is treated as rate-limit and retried."""
        from openai import APIStatusError

        orch = _make_orchestrator()
        good_resp = _make_openai_text_response("OK")

        err_resp = MagicMock()
        err_resp.status_code = 429
        err_resp.headers = {}
        api_err = APIStatusError(
            message="Too many requests",
            response=err_resp,
            body=None,
        )

        orch.client.chat.completions.create = AsyncMock(
            side_effect=[api_err, good_resp]
        )

        with patch("services.ai_orchestrator.asyncio.sleep", new_callable=AsyncMock):
            result = await orch.analyze(messages=[{"role": "user", "content": "test"}])

        assert result["type"] == "text"

    @pytest.mark.asyncio
    async def test_analyze_api_status_error_non_429_returns_error(self):
        """APIStatusError with non-429 code returns error immediately."""
        from openai import APIStatusError

        orch = _make_orchestrator()
        err_resp = MagicMock()
        err_resp.status_code = 500
        err_resp.headers = {}
        api_err = APIStatusError(
            message="Internal server error",
            response=err_resp,
            body=None,
        )

        orch.client.chat.completions.create = AsyncMock(side_effect=api_err)

        result = await orch.analyze(messages=[{"role": "user", "content": "test"}])
        assert result["type"] == "error"
        assert "greske" in result["content"].lower() or "komunikacij" in result["content"].lower()

    @pytest.mark.asyncio
    async def test_analyze_generic_exception_returns_error(self):
        """Unexpected exceptions produce an error result."""
        orch = _make_orchestrator()
        orch.client.chat.completions.create = AsyncMock(
            side_effect=ValueError("Something broke")
        )

        result = await orch.analyze(messages=[{"role": "user", "content": "test"}])
        assert result["type"] == "error"
        assert "greske" in result["content"].lower() or "neocekivan" in result["content"].lower()


# ============================================================================
# 4. _calculate_backoff tests
# ============================================================================

class TestCalculateBackoff:
    def test_backoff_attempt_0(self):
        orch = _make_orchestrator()
        with patch("services.retry_utils.random.uniform", return_value=0.25):
            delay = orch._calculate_backoff(0)
        # 2^0 * 1.0 + 0.25 = 1.25
        assert delay == pytest.approx(1.25)

    def test_backoff_attempt_1(self):
        orch = _make_orchestrator()
        with patch("services.retry_utils.random.uniform", return_value=0.0):
            delay = orch._calculate_backoff(1)
        # 2^1 * 1.0 + 0.0 = 2.0
        assert delay == pytest.approx(2.0)

    def test_backoff_attempt_2(self):
        orch = _make_orchestrator()
        with patch("services.retry_utils.random.uniform", return_value=0.5):
            delay = orch._calculate_backoff(2)
        # 2^2 * 1.0 + 0.5 = 4.5
        assert delay == pytest.approx(4.5)


# ============================================================================
# 5. _handle_rate_limit / _handle_timeout tests
# ============================================================================

class TestHandleRateLimitAndTimeout:
    @pytest.mark.asyncio
    async def test_handle_rate_limit_retries_when_not_last_attempt(self):
        orch = _make_orchestrator()
        with patch("services.ai_orchestrator.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await orch._handle_rate_limit(0, "RateLimitError")
        assert result is None  # means "continue retrying"
        mock_sleep.assert_called_once()
        assert orch._rate_limit_hits == 1

    @pytest.mark.asyncio
    async def test_handle_rate_limit_returns_error_on_last_attempt(self):
        orch = _make_orchestrator()
        result = await orch._handle_rate_limit(2, "RateLimitError")  # attempt 2 = last (MAX_RETRIES=3)
        assert result is not None
        assert result["type"] == "error"

    @pytest.mark.asyncio
    async def test_handle_timeout_retries_when_not_last_attempt(self):
        orch = _make_orchestrator()
        with patch("services.ai_orchestrator.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            result = await orch._handle_timeout(0)
        assert result is None
        mock_sleep.assert_called_once()

    @pytest.mark.asyncio
    async def test_handle_timeout_returns_error_on_last_attempt(self):
        orch = _make_orchestrator()
        result = await orch._handle_timeout(2)
        assert result is not None
        assert result["type"] == "error"
        assert "nije odgovorio" in result["content"]


# ============================================================================
# 6. get_retry_status / get_token_stats tests
# ============================================================================

class TestStats:
    def test_get_retry_status_default_none(self):
        orch = _make_orchestrator()
        assert orch.get_retry_status() is None

    def test_get_token_stats_initial(self):
        orch = _make_orchestrator()
        stats = orch.get_token_stats()
        assert stats["total_prompt_tokens"] == 0
        assert stats["total_completion_tokens"] == 0
        assert stats["total_tokens"] == 0
        assert stats["total_requests"] == 0
        assert stats["rate_limit_hits"] == 0
        assert stats["avg_tokens_per_request"] == 0

    def test_get_token_stats_after_usage(self):
        orch = _make_orchestrator()
        orch._total_prompt_tokens = 500
        orch._total_completion_tokens = 200
        orch._total_requests = 2
        orch._rate_limit_hits = 1

        stats = orch.get_token_stats()
        assert stats["total_tokens"] == 700
        assert stats["avg_tokens_per_request"] == 350.0
        assert stats["rate_limit_hits"] == 1


# ============================================================================
# 7. _count_tokens tests
# ============================================================================

class TestCountTokens:
    def test_count_tokens_fallback_no_tokenizer(self):
        """Fallback counting: chars/4.6 + overhead."""
        orch = _make_orchestrator()
        orch.tokenizer = None

        messages = [
            {"role": "user", "content": "Hello world"},  # 11 chars
        ]
        count = orch._count_tokens(messages)
        expected = int(11 / 4.6) + 1 * 3  # int(2.39) + 3 = 2 + 3 = 5
        assert count == expected

    def test_count_tokens_fallback_empty_messages(self):
        orch = _make_orchestrator()
        orch.tokenizer = None
        count = orch._count_tokens([])
        assert count == 0

    def test_count_tokens_with_tokenizer(self):
        """When tokenizer is available, use encode()."""
        orch = _make_orchestrator()
        mock_tokenizer = MagicMock()
        mock_tokenizer.encode.return_value = [1, 2, 3, 4, 5]  # 5 tokens
        orch.tokenizer = mock_tokenizer

        messages = [
            {"role": "user", "content": "test"},
        ]
        count = orch._count_tokens(messages)
        # 3 (message overhead) + 5 (role) + 5 (content) + 3 (final overhead) = 16
        # Actually the method iterates over all key/value pairs in each message dict
        # role="user" => encode("user") = [1,2,3,4,5] = 5 tokens
        # content="test" => encode("test") = [1,2,3,4,5] = 5 tokens
        # + 3 per message + 3 final = 3 + 5 + 5 + 3 = 16
        assert count == 16

    def test_count_tokens_fallback_multiple_messages(self):
        orch = _make_orchestrator()
        orch.tokenizer = None

        messages = [
            {"role": "user", "content": "AAAA"},      # 4 chars
            {"role": "assistant", "content": "BBBB"},  # 4 chars
        ]
        count = orch._count_tokens(messages)
        # total_chars = 8, int(8/4.6) = 1, + 2*3 = 7
        expected = int(8 / 4.6) + 2 * 3
        assert count == expected


# ============================================================================
# 8. _apply_token_budgeting tests
# ============================================================================

class TestApplyTokenBudgeting:
    def test_returns_none_when_tools_none(self):
        orch = _make_orchestrator()
        assert orch._apply_token_budgeting(None, None) is None

    def test_returns_tools_unchanged_when_no_scores_and_small(self):
        orch = _make_orchestrator()
        tools = [{"type": "function", "function": {"name": f"t{i}"}} for i in range(5)]
        result = orch._apply_token_budgeting(tools, None)
        assert result == tools

    def test_trims_when_no_scores_and_over_limit(self):
        orch = _make_orchestrator()
        tools = [{"type": "function", "function": {"name": f"t{i}"}} for i in range(30)]
        result = orch._apply_token_budgeting(tools, None)
        assert len(result) == 25  # MAX_TOOLS_FOR_LLM default

    def test_mismatched_tools_and_scores_returns_unchanged(self):
        orch = _make_orchestrator()
        tools = [
            {"type": "function", "function": {"name": "t0"}},
            {"type": "function", "function": {"name": "t1"}},
            {"type": "function", "function": {"name": "t2"}},
        ]
        scores = [
            {"name": "t0", "score": 0.9},
            {"name": "t1", "score": 0.8},
        ]
        result = orch._apply_token_budgeting(tools, scores)
        assert result == tools

    def test_small_list_with_scores_returns_unchanged(self):
        """Tools within MAX_TOOLS_FOR_LLM are returned unchanged."""
        orch = _make_orchestrator()
        tools = [{"type": "function", "function": {"name": f"t{i}"}} for i in range(6)]
        scores = [
            {"name": "t0", "score": 0.95},
            {"name": "t1", "score": 0.9},
            {"name": "t2", "score": 0.8},
            {"name": "t3", "score": 0.7},
            {"name": "t4", "score": 0.6},
            {"name": "t5", "score": 0.5},
        ]
        result = orch._apply_token_budgeting(tools, scores)
        assert len(result) == 6


# ============================================================================
# 9. _apply_smart_history tests
# ============================================================================

class TestApplySmartHistory:
    def test_returns_messages_unchanged_when_under_token_limit(self):
        orch = _make_orchestrator()
        orch.tokenizer = None
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hello"},
        ]
        result = orch._apply_smart_history(messages)
        assert result == messages

    def test_trims_long_conversation(self):
        """When token count exceeds limit, history is trimmed and summarized."""
        orch = _make_orchestrator()
        orch.tokenizer = None

        # Create a conversation that exceeds MAX_TOKEN_LIMIT (8000)
        # With fallback: chars/4.6 + messages*3
        # Each message ~200 chars => ~43 tokens + 3 overhead = ~46 tokens per msg
        # Need 8000/46 ~ 174 messages. Let's use 200 to be safe.
        messages = [{"role": "system", "content": "System prompt " * 10}]
        for i in range(200):
            role = "user" if i % 2 == 0 else "assistant"
            messages.append({"role": role, "content": f"Message number {i} with some content " * 5})

        result = orch._apply_smart_history(messages)
        # Should be shorter than original
        assert len(result) < len(messages)

    def test_smart_history_preserves_system_message(self):
        """System message is preserved at start after summarization."""
        orch = _make_orchestrator()
        orch.tokenizer = None

        messages = [{"role": "system", "content": "Important system prompt"}]
        for i in range(200):
            role = "user" if i % 2 == 0 else "assistant"
            messages.append({"role": role, "content": f"Detailed message {i} " * 10})

        result = orch._apply_smart_history(messages)
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "Important system prompt"

    def test_smart_history_no_system_message(self):
        """When no system message, conversation still gets trimmed."""
        orch = _make_orchestrator()
        orch.tokenizer = None

        messages = []
        for i in range(200):
            role = "user" if i % 2 == 0 else "assistant"
            messages.append({"role": role, "content": f"Detailed message {i} " * 10})

        result = orch._apply_smart_history(messages)
        assert len(result) < len(messages)


# ============================================================================
# 10. _extract_entities tests
# ============================================================================

class TestExtractEntities:
    def test_extract_vehicle_uuid(self):
        orch = _make_orchestrator()
        messages = [
            {"role": "user", "content": "Check vehicle 123e4567-e89b-12d3-a456-426614174000"}
        ]
        entities = orch._extract_entities(messages)
        assert "123e4567-e89b-12d3-a456-426614174000" in entities["VehicleId"]

    def test_extract_person_uuid(self):
        orch = _make_orchestrator()
        messages = [
            {"role": "user", "content": "Find person 123e4567-e89b-12d3-a456-426614174000"}
        ]
        entities = orch._extract_entities(messages)
        assert "123e4567-e89b-12d3-a456-426614174000" in entities["PersonId"]

    def test_extract_booking_uuid(self):
        orch = _make_orchestrator()
        messages = [
            {"role": "user", "content": "Cancel booking 123e4567-e89b-12d3-a456-426614174000"}
        ]
        entities = orch._extract_entities(messages)
        assert "123e4567-e89b-12d3-a456-426614174000" in entities["BookingId"]

    def test_extract_croatian_plate(self):
        orch = _make_orchestrator()
        messages = [
            {"role": "user", "content": "Registracija je ZG-1234-AB"}
        ]
        entities = orch._extract_entities(messages)
        assert len(entities["LicencePlate"]) >= 1

    def test_extract_multiple_uuids(self):
        orch = _make_orchestrator()
        messages = [
            {"role": "user", "content": "Transfer vehicle 123e4567-e89b-12d3-a456-426614174000 to 234f5678-f90a-12d3-a456-426614174001"}
        ]
        entities = orch._extract_entities(messages)
        assert len(entities["VehicleId"]) == 2

    def test_extract_empty_message(self):
        orch = _make_orchestrator()
        messages = [{"role": "user", "content": ""}]
        entities = orch._extract_entities(messages)
        assert entities["VehicleId"] == []
        assert entities["PersonId"] == []
        assert entities["BookingId"] == []
        assert entities["LicencePlate"] == []

    def test_extract_no_content_key(self):
        orch = _make_orchestrator()
        messages = [{"role": "user"}]  # missing "content"
        entities = orch._extract_entities(messages)
        assert all(len(v) == 0 for v in entities.values())


# ============================================================================
# 11. _format_entity_context tests
# ============================================================================

class TestFormatEntityContext:
    def test_format_with_values(self):
        orch = _make_orchestrator()
        entities = {
            "VehicleId": ["uuid-1", "uuid-2"],
            "PersonId": ["uuid-3"],
            "BookingId": [],
            "LicencePlate": ["ZG-123-AB"],
        }
        result = orch._format_entity_context(entities)
        assert "VehicleId=uuid-1,uuid-2" in result
        assert "PersonId=uuid-3" in result
        assert "BookingId" not in result
        assert "LicencePlate=ZG-123-AB" in result

    def test_format_all_empty(self):
        orch = _make_orchestrator()
        entities = {"VehicleId": [], "PersonId": [], "BookingId": [], "LicencePlate": []}
        result = orch._format_entity_context(entities)
        assert result == ""


# ============================================================================
# 12. _summarize_conversation tests
# ============================================================================

class TestSummarizeConversation:
    def test_summarize_basic(self):
        orch = _make_orchestrator()
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi"},
        ]
        summary = orch._summarize_conversation(messages)
        assert "2 poruka" in summary
        assert "1 user" in summary
        assert "1 assistant" in summary

    def test_summarize_no_tool_no_crash(self):
        """No tool role in messages should not cause KeyError."""
        orch = _make_orchestrator()
        messages = [
            {"role": "user", "content": "Test"},
            {"role": "assistant", "content": "OK"},
        ]
        summary = orch._summarize_conversation(messages)
        assert "0 tool" in summary.lower()


# ============================================================================
# 13. extract_parameters tests
# ============================================================================

class TestExtractParameters:
    @pytest.mark.asyncio
    async def test_extract_parameters_success(self):
        orch = _make_orchestrator()
        mock_resp = _make_openai_text_response('{"FromTime": "2025-01-15T08:00:00"}')
        orch.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        result = await orch.extract_parameters(
            user_input="Sutra od 8 ujutro",
            required_params=[
                {"name": "FromTime", "type": "string", "description": "Start time"},
            ],
        )
        assert result == {"FromTime": "2025-01-15T08:00:00"}

    @pytest.mark.asyncio
    async def test_extract_parameters_strips_markdown(self):
        """JSON wrapped in ```json ... ``` is handled correctly."""
        orch = _make_orchestrator()
        mock_resp = _make_openai_text_response('```json\n{"key": "value"}\n```')
        orch.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        result = await orch.extract_parameters(
            user_input="test",
            required_params=[{"name": "key", "type": "string"}],
        )
        assert result == {"key": "value"}

    @pytest.mark.asyncio
    async def test_extract_parameters_invalid_json(self):
        """Invalid JSON from LLM returns empty dict."""
        orch = _make_orchestrator()
        mock_resp = _make_openai_text_response("NOT JSON AT ALL")
        orch.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        result = await orch.extract_parameters(
            user_input="test",
            required_params=[{"name": "x", "type": "string"}],
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_extract_parameters_with_context(self):
        """Additional context is appended to system prompt."""
        orch = _make_orchestrator()
        mock_resp = _make_openai_text_response('{"VehicleId": "abc"}')
        orch.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        result = await orch.extract_parameters(
            user_input="that one",
            required_params=[{"name": "VehicleId", "type": "string"}],
            context="Previous vehicle was abc",
        )
        assert result == {"VehicleId": "abc"}

    @pytest.mark.asyncio
    async def test_extract_parameters_rate_limit_retries(self):
        """Rate limit during parameter extraction is retried."""
        from openai import RateLimitError

        orch = _make_orchestrator()
        good_resp = _make_openai_text_response('{"key": "val"}')

        rate_err = RateLimitError(
            message="rate limit",
            response=MagicMock(status_code=429, headers={}),
            body=None,
        )

        orch.client.chat.completions.create = AsyncMock(
            side_effect=[rate_err, good_resp]
        )

        with patch("services.ai_orchestrator.asyncio.sleep", new_callable=AsyncMock):
            result = await orch.extract_parameters(
                user_input="test",
                required_params=[{"name": "key", "type": "string"}],
            )
        assert result == {"key": "val"}

    @pytest.mark.asyncio
    async def test_extract_parameters_timeout_retries(self):
        """Timeout during parameter extraction is retried."""
        from openai import APITimeoutError

        orch = _make_orchestrator()
        good_resp = _make_openai_text_response('{"key": "val"}')
        timeout_err = APITimeoutError(request=MagicMock())

        orch.client.chat.completions.create = AsyncMock(
            side_effect=[timeout_err, good_resp]
        )

        with patch("services.ai_orchestrator.asyncio.sleep", new_callable=AsyncMock):
            result = await orch.extract_parameters(
                user_input="test",
                required_params=[{"name": "key", "type": "string"}],
            )
        assert result == {"key": "val"}

    @pytest.mark.asyncio
    async def test_extract_parameters_generic_exception(self):
        """Generic exception returns empty dict."""
        orch = _make_orchestrator()
        orch.client.chat.completions.create = AsyncMock(
            side_effect=RuntimeError("boom")
        )

        result = await orch.extract_parameters(
            user_input="test",
            required_params=[{"name": "key", "type": "string"}],
        )
        assert result == {}


# ============================================================================
# 14. build_system_prompt tests
# ============================================================================

class TestBuildSystemPrompt:
    def test_build_prompt_includes_user_name(self):
        orch = _make_orchestrator()
        with patch("services.ai_orchestrator.UserContextManager") as MockCtx:
            ctx = MockCtx.return_value
            ctx.display_name = "Ivan"
            ctx.person_id = "abc-12345678-xyz"
            ctx.vehicle = None

            prompt = orch.build_system_prompt({"display_name": "Ivan"})
            assert "Ivan" in prompt

    def test_build_prompt_includes_vehicle_info(self):
        orch = _make_orchestrator()
        with patch("services.ai_orchestrator.UserContextManager") as MockCtx:
            ctx = MockCtx.return_value
            ctx.display_name = "Marko"
            ctx.person_id = "person-12345678-abcd"
            mock_vehicle = MagicMock()
            mock_vehicle.plate = "ZG-1234-AB"
            mock_vehicle.name = "VW Passat"
            mock_vehicle.mileage = "50000"
            ctx.vehicle = mock_vehicle

            prompt = orch.build_system_prompt({"display_name": "Marko"})
            assert "ZG-1234-AB" in prompt
            assert "VW Passat" in prompt
            assert "50000" in prompt

    def test_build_prompt_with_flow_context(self):
        orch = _make_orchestrator()
        with patch("services.ai_orchestrator.UserContextManager") as MockCtx:
            ctx = MockCtx.return_value
            ctx.display_name = "Ana"
            ctx.person_id = "person-12345678-efgh"
            ctx.vehicle = None

            flow = {
                "current_flow": "booking",
                "state": "collecting_params",
                "parameters": {"FromTime": "2025-01-15"},
                "missing_params": ["ToTime"],
            }
            prompt = orch.build_system_prompt({"display_name": "Ana"}, flow_context=flow)
            assert "booking" in prompt
            assert "collecting_params" in prompt
            assert "ToTime" in prompt

    def test_build_prompt_no_flow_context(self):
        orch = _make_orchestrator()
        with patch("services.ai_orchestrator.UserContextManager") as MockCtx:
            ctx = MockCtx.return_value
            ctx.display_name = "Test"
            ctx.person_id = "person-12345678-1234"
            ctx.vehicle = None

            prompt = orch.build_system_prompt({})
            assert "TRENUTNI TOK" not in prompt


# ============================================================================
# 15. Edge cases and integration-like scenarios
# ============================================================================

class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_analyze_usage_none_does_not_crash(self):
        """When response.usage is None, no token tracking crash."""
        orch = _make_orchestrator()

        mock_choice = MagicMock()
        mock_choice.message.content = "ok"
        mock_choice.message.tool_calls = None

        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_resp.usage = None
        # hasattr will return True because it's a MagicMock, but usage is None
        # The code checks: if hasattr(response, 'usage') and response.usage:
        orch.client.chat.completions.create = AsyncMock(return_value=mock_resp)

        result = await orch.analyze(messages=[{"role": "user", "content": "test"}])
        assert result["type"] == "text"
        assert result["usage"] is None

    def test_count_tokens_message_with_none_content(self):
        """Message with None content doesn't crash token counting."""
        orch = _make_orchestrator()
        orch.tokenizer = None
        messages = [{"role": "assistant", "content": None}]
        result = orch._count_tokens(messages)
        assert result >= 0

    @pytest.mark.asyncio
    async def test_analyze_multiple_tool_calls_picks_first(self):
        """When LLM returns multiple tool_calls, only the first is used."""
        orch = _make_orchestrator()

        tc1 = MagicMock()
        tc1.function.name = "tool_1"
        tc1.function.arguments = '{"a": 1}'
        tc1.id = "call_1"

        tc2 = MagicMock()
        tc2.function.name = "tool_2"
        tc2.function.arguments = '{"b": 2}'
        tc2.id = "call_2"

        mock_choice = MagicMock()
        mock_choice.message.content = None
        mock_choice.message.tool_calls = [tc1, tc2]

        mock_resp = MagicMock()
        mock_resp.choices = [mock_choice]
        mock_resp.usage.prompt_tokens = 50
        mock_resp.usage.completion_tokens = 20

        orch.client.chat.completions.create = AsyncMock(return_value=mock_resp)
        tools = [
            {"type": "function", "function": {"name": "tool_1", "parameters": {}}},
            {"type": "function", "function": {"name": "tool_2", "parameters": {}}},
        ]

        result = await orch.analyze(
            messages=[{"role": "user", "content": "test"}],
            tools=tools,
        )
        assert result["tool"] == "tool_1"
        assert result["tool_call_id"] == "call_1"

    def test_token_budgeting_empty_tools_list(self):
        orch = _make_orchestrator()
        result = orch._apply_token_budgeting([], None)
        assert result == []

    def test_smart_history_single_message(self):
        orch = _make_orchestrator()
        orch.tokenizer = None
        messages = [{"role": "user", "content": "Hi"}]
        result = orch._apply_smart_history(messages)
        assert result == messages
