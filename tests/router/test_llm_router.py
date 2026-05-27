"""Unit tests for LLMRouter.

The LLM is mocked — these tests verify routing logic (top-K retrieval,
tool-call parsing, validation, error handling). End-to-end accuracy with
real Azure is tested separately via scripts/bench_full_coverage.py.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from services.router.anchor_index import AnchorIndex
from services.router.llm_router import LLMRouter, RouterResult
from services.router.tool_schema_builder import ToolSchemaBuilder


# ---- Fakes ---------------------------------------------------------------


def _hashed_embed(texts):
    out = []
    for t in texts:
        h = hashlib.md5(t.encode("utf-8")).digest()
        out.append([b / 255.0 for b in h[:8]])
    return out


async def _embed_fn(texts):
    return _hashed_embed(texts)


@dataclass
class _FakeToolCall:
    function: object


@dataclass
class _FakeFunction:
    name: str
    arguments: str


@dataclass
class _FakeMessage:
    content: str = ""
    tool_calls: list = None  # populated post-init


@dataclass
class _FakeChoice:
    message: _FakeMessage


@dataclass
class _FakeResponse:
    choices: list


class _FakeCompletions:
    """Async stub for client.chat.completions.create()."""

    def __init__(self):
        self.queue: list = []
        self.last_kwargs: dict | None = None

    def queue_tool_call(self, function_name: str, arguments: dict):
        msg = _FakeMessage(
            content="",
            tool_calls=[
                _FakeToolCall(
                    function=_FakeFunction(
                        name=function_name,
                        arguments=json.dumps(arguments),
                    )
                )
            ],
        )
        self.queue.append(_FakeResponse(choices=[_FakeChoice(message=msg)]))

    def queue_no_tool_call(self, content: str = ""):
        msg = _FakeMessage(content=content, tool_calls=[])
        self.queue.append(_FakeResponse(choices=[_FakeChoice(message=msg)]))

    def queue_error(self, exc: Exception):
        self.queue.append(exc)

    async def create(self, **kwargs):
        self.last_kwargs = kwargs
        if not self.queue:
            raise RuntimeError("test queue empty — add fakes")
        item = self.queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class _FakeLLM:
    def __init__(self):
        self.completions = _FakeCompletions()
        self.chat = _FakeChat(self.completions)


# ---- Fixtures ------------------------------------------------------------


def _registry_2_tools():
    return {
        "tools": [
            {
                "operation_id": "get_MasterData",
                "method": "GET",
                "path": "/automation/MasterData",
                "parameters": {
                    "Rows": {
                        "name": "Rows", "param_type": "integer",
                        "description": "Number of rows to return",
                        "required": False, "dependency_source": "user_input",
                    },
                    "personId": {
                        "name": "personId", "param_type": "string",
                        "description": "Person ID (auto)",
                        "required": True, "dependency_source": "context",
                    },
                },
                "required_params": [],
                "is_mutation": False,
            },
            {
                "operation_id": "post_AddCase",
                "method": "POST",
                "path": "/automation/AddCase",
                "parameters": {
                    "Subject": {
                        "name": "Subject", "param_type": "string",
                        "description": "Title", "required": True,
                        "dependency_source": "user_input",
                    },
                    "Message": {
                        "name": "Message", "param_type": "string",
                        "description": "Body", "required": True,
                        "dependency_source": "user_input",
                    },
                },
                "required_params": ["Subject", "Message"],
                "is_mutation": True,
            },
        ]
    }


def _tkb_2_tools():
    return {
        "get_MasterData": {
            "intent_summary": "Snapshot vozila vozača",
            "use_when": ["korisnik pita o autu"],
        },
        "post_AddCase": {
            "intent_summary": "Otvori novi slučaj kvara/štete",
            "use_when": ["prijavljujem štetu"],
        },
    }


async def _build_router(tmp_path, anchors):
    """Helper: build AnchorIndex + ToolSchemaBuilder + LLMRouter."""
    apath = tmp_path / "anchors.json"
    apath.write_text(json.dumps(anchors), encoding="utf-8")
    idx = AnchorIndex(anchors_path=apath, cache_path=None)
    registry = _registry_2_tools()
    tkb = _tkb_2_tools()
    schema_builder = ToolSchemaBuilder.from_registry(registry)
    llm = _FakeLLM()
    router = LLMRouter(
        anchor_index=idx,
        schema_builder=schema_builder,
        registry=registry,
        tkb=tkb,
        llm_client=llm,
        embed_fn=_embed_fn,
        deployment_name="test-model",
    )
    await router.initialize()
    return router, llm


# ---- Tests ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_query_returns_error(tmp_path):
    router, _ = await _build_router(tmp_path, {
        "get_MasterData": ["fraza"],
        "post_AddCase": ["fraza"],
    })
    r = await router.route("")
    assert r.error == "empty_query"
    assert r.tool_id is None


@pytest.mark.asyncio
async def test_happy_path_returns_tool_and_params(tmp_path):
    router, llm = await _build_router(tmp_path, {
        "get_MasterData": ["moja kilometraža"],
        "post_AddCase": ["prijavi kvar"],
    })
    llm.completions.queue_tool_call("get_MasterData", {"Rows": 1})

    r = await router.route("kolika mi je kilometraža")
    assert r.tool_id == "get_MasterData"
    assert r.params == {"Rows": 1}
    assert r.error is None
    # Confidence non-zero
    assert r.confidence > 0.5
    # tool_choice=required (force a pick from top-K) and tools passed
    assert llm.completions.last_kwargs["tool_choice"] == "required"
    assert len(llm.completions.last_kwargs["tools"]) == 2


@pytest.mark.asyncio
async def test_llm_refuses_to_call_tool_returns_alternatives(tmp_path):
    router, llm = await _build_router(tmp_path, {
        "get_MasterData": ["foo"],
        "post_AddCase": ["bar"],
    })
    llm.completions.queue_no_tool_call(content="Ne razumijem zahtjev.")

    r = await router.route("kompletno nepoznato")
    assert r.tool_id is None
    assert r.error == "no_tool_call"
    assert "Ne razumijem" in r.rationale
    # Alternatives surfaced from anchor retrieval
    assert len(r.alternatives) >= 1
    assert r.alternatives[0]["tool_id"] in {"get_MasterData", "post_AddCase"}


@pytest.mark.asyncio
async def test_hallucinated_tool_rejected(tmp_path):
    router, llm = await _build_router(tmp_path, {
        "get_MasterData": ["foo"],
        "post_AddCase": ["bar"],
    })
    llm.completions.queue_tool_call("get_NonExistent", {})

    r = await router.route("query")
    assert r.tool_id is None
    assert r.error and r.error.startswith("hallucinated_tool:")
    assert r.alternatives  # surface what anchor retrieved instead


@pytest.mark.asyncio
async def test_invalid_args_json_returns_tool_with_empty_params(tmp_path):
    router, llm = await _build_router(tmp_path, {
        "get_MasterData": ["foo"],
        "post_AddCase": ["bar"],
    })
    msg = _FakeMessage(
        content="",
        tool_calls=[_FakeToolCall(function=_FakeFunction(
            name="get_MasterData", arguments="not-json-{{{",
        ))],
    )
    llm.completions.queue.append(
        _FakeResponse(choices=[_FakeChoice(message=msg)])
    )

    r = await router.route("query")
    assert r.tool_id == "get_MasterData"
    assert r.params == {}
    assert r.error is None  # tool selection succeeded; params just empty


@pytest.mark.asyncio
async def test_missing_required_params_surfaced(tmp_path):
    router, llm = await _build_router(tmp_path, {
        "get_MasterData": ["foo"],
        "post_AddCase": ["prijavi kvar"],
    })
    # LLM picks AddCase but only fills Subject, missing Message
    llm.completions.queue_tool_call("post_AddCase", {"Subject": "kvar"})

    r = await router.route("prijavljujem kvar")
    assert r.tool_id == "post_AddCase"
    assert "Message" in r.missing_required


@pytest.mark.asyncio
async def test_llm_exception_returns_error(tmp_path):
    router, llm = await _build_router(tmp_path, {
        "get_MasterData": ["foo"],
        "post_AddCase": ["bar"],
    })
    llm.completions.queue_error(RuntimeError("Azure 500"))

    r = await router.route("query")
    assert r.tool_id is None
    assert r.error and r.error.startswith("llm_error:")


@pytest.mark.asyncio
async def test_long_tool_name_alias_round_trip(tmp_path):
    """If the LLM returns an alias, router resolves it back to operation_id."""
    long_op = "put_PeriodicActivitiesSchedules_id_documents_documentId_SetAsDefault"
    registry = {
        "tools": [{
            "operation_id": long_op, "method": "PUT", "path": "/x",
            "parameters": {}, "required_params": [], "is_mutation": True,
        }]
    }
    tkb = {long_op: {"intent_summary": "Set as default"}}
    apath = tmp_path / "anchors.json"
    apath.write_text(json.dumps({long_op: ["postavi kao zadani"]}), encoding="utf-8")
    idx = AnchorIndex(anchors_path=apath, cache_path=None)
    schema_builder = ToolSchemaBuilder.from_registry(registry)
    llm = _FakeLLM()
    router = LLMRouter(
        anchor_index=idx, schema_builder=schema_builder,
        registry=registry, tkb=tkb, llm_client=llm, embed_fn=_embed_fn,
        deployment_name="test",
    )
    await router.initialize()
    alias = schema_builder.schema_name_for(long_op)
    assert len(alias) <= 64
    llm.completions.queue_tool_call(alias, {})

    r = await router.route("query")
    assert r.tool_id == long_op  # alias resolved back


@pytest.mark.asyncio
async def test_history_and_identity_passed_to_llm(tmp_path):
    router, llm = await _build_router(tmp_path, {
        "get_MasterData": ["foo"],
        "post_AddCase": ["bar"],
    })
    llm.completions.queue_tool_call("get_MasterData", {})

    await router.route(
        "kolika km",
        identity_summary="Filip, vozilo DA053F",
        conversation_history=[
            {"user": "bok", "bot": "Bok Filip!"},
            {"user": "registracija?", "bot": "DA053F"},
        ],
    )

    messages = llm.completions.last_kwargs["messages"]
    user_msg = messages[-1]["content"]
    assert "Filip, vozilo DA053F" in user_msg
    assert "registracija?" in user_msg  # last turn surfaced


@pytest.mark.asyncio
async def test_alternatives_excludes_picked_tool(tmp_path):
    router, llm = await _build_router(tmp_path, {
        "get_MasterData": ["fraza1"],
        "post_AddCase": ["fraza2"],
    })
    llm.completions.queue_tool_call("get_MasterData", {})

    r = await router.route("query")
    assert all(a["tool_id"] != "get_MasterData" for a in r.alternatives)
