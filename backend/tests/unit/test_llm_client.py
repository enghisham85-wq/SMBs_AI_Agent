"""LLM client settings: the model, room for thinking, and replay fixtures tied to model and effort."""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from typing import Any

import pytest

from app.llm import client
from app.llm.client import LLMClient, LLMUnavailableError, document_block, request_hash, text_block
from app.llm.schemas import OwnerMessage


def test_uses_claude_opus_5_5_with_room_for_thinking() -> None:
    assert client.MODEL == "claude-opus-5-5"
    assert client.DEFAULT_MAX_TOKENS >= 16000  # thinking is always on and counts toward max_tokens
    assert set(client.EFFORT) >= {"extraction", "verifier", "classification", "message", "incident"}  # never the default


def test_fixture_key_changes_with_model_or_effort(monkeypatch: Any) -> None:
    args = ("extraction", "Read the invoice.", [text_block("x")], "InvoiceExtraction")
    base = request_hash(*args)
    monkeypatch.setattr(client, "MODEL", "another-model")
    assert request_hash(*args) != base
    monkeypatch.setattr(client, "MODEL", "claude-opus-5-5")
    monkeypatch.setitem(client.EFFORT, "extraction", "low")
    assert request_hash(*args) != base


def test_cache_markers_do_not_change_the_fixture_key() -> None:
    plain = request_hash("classification", "p", [text_block("chart"), text_block("line")], "ExpenseClassification")
    marked = request_hash("classification", "p", [text_block("chart", cache=True), text_block("line")],
                          "ExpenseClassification")
    assert plain == marked


class _FakeMessages:
    def __init__(self, delay: float = 0.0) -> None:
        self.delay = delay
        self.kwargs: dict[str, Any] = {}

    async def parse(self, **kwargs: Any) -> Any:
        self.kwargs = kwargs
        await asyncio.sleep(self.delay)
        return SimpleNamespace(stop_reason="end_turn", parsed_output=OwnerMessage(text_en="Hi", text_ar="مرحبا"),
                               usage=SimpleNamespace(input_tokens=10, cache_read_input_tokens=0,
                                                     cache_creation_input_tokens=0, output_tokens=5))


def _live(messages: _FakeMessages) -> LLMClient:
    llm = LLMClient(mode="live")
    llm._client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    return llm


async def test_documents_go_out_base64_with_the_callers_cache_marker_only() -> None:
    fake = _FakeMessages()
    doc = document_block(b"%PDF-1.7 bytes", "application/pdf", cache=True)
    await _live(fake).parse("extraction", "Read it.", [doc, text_block("Attempt 1.")], OwnerMessage,
                            offline=lambda: OwnerMessage(text_en="", text_ar=""))
    sent = fake.kwargs["messages"][0]["content"]
    assert sent[0]["source"]["data"] == base64.b64encode(b"%PDF-1.7 bytes").decode("ascii")
    assert sent[0]["cache_control"] == {"type": "ephemeral"} and "cache_control" not in sent[1]
    assert "cache_control" not in fake.kwargs  # no top-level marker on the unique tail
    assert doc["source"]["data"] == b"%PDF-1.7 bytes"  # the caller's block is left as it was


async def test_a_call_past_its_deadline_counts_as_unavailable(monkeypatch: Any) -> None:
    monkeypatch.setitem(client.DEADLINE_S, "message", 0.05)
    with pytest.raises(LLMUnavailableError):
        await _live(_FakeMessages(delay=5)).parse("message", "Word it.", [text_block("{}")], OwnerMessage,
                                                  offline=lambda: OwnerMessage(text_en="", text_ar=""))
