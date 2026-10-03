"""LLM client settings: the model, room for thinking, and replay fixtures tied to model and effort."""

from __future__ import annotations

import asyncio
import base64
import json
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


# OpenAI-compatible gateway
class _FakeCompletions:
    def __init__(self, replies: list[str], finish_reason: str = "stop") -> None:
        self.replies, self.finish_reason = list(replies), finish_reason
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        message = SimpleNamespace(content=self.replies.pop(0), refusal=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=self.finish_reason)],
                               usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5))


def _gateway(monkeypatch: Any, completions: _FakeCompletions) -> LLMClient:
    from app.config import get_settings

    for name, value in {"LLM_PROVIDER": "openai_compatible", "LLM_BASE_URL": "https://gateway.example/v1",
                        "LLM_API_KEY": "test-key", "LLM_MODEL": "claude-opus-5.5"}.items():
        monkeypatch.setattr(get_settings(), name, value)
    llm = LLMClient(mode="live")
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return llm


_OFFLINE = lambda: OwnerMessage(text_en="", text_ar="")  # noqa: E731


async def test_gateway_gets_text_image_and_pdf_parts_and_the_schema_in_json_mode(monkeypatch: Any) -> None:
    fake = _FakeCompletions(['{"text_en": "Hi", "text_ar": "مرحبا"}'])
    pdf, png = document_block(b"%PDF-1.7", "application/pdf", cache=True), document_block(b"\x89PNG", "image/png")
    out = await _gateway(monkeypatch, fake).parse("extraction", "Read it.", [pdf, png, text_block("Attempt 1.")],
                                                  OwnerMessage, offline=_OFFLINE)
    assert out == OwnerMessage(text_en="Hi", text_ar="مرحبا")
    call = fake.calls[0]
    assert call["model"] == "claude-opus-5.5"
    system = call["messages"][0]
    assert system["role"] == "system" and system["content"].startswith("Read it.")
    assert json.dumps(OwnerMessage.model_json_schema(), ensure_ascii=False) in system["content"]
    parts = call["messages"][1]["content"]
    assert parts[0] == {"type": "file", "file": {"filename": "document.pdf", "file_data": "data:application/pdf;base64,"
                                                 + base64.b64encode(b"%PDF-1.7").decode("ascii")}}
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert parts[2] == {"type": "text", "text": "Attempt 1."}  # cache markers have no equivalent and are dropped
    assert call["response_format"] == {"type": "json_object"}
    assert call["extra_body"] == {"reasoning": {"effort": "high"}}


async def test_gateway_reply_in_a_code_fence_still_parses(monkeypatch: Any) -> None:
    fake = _FakeCompletions(['```json\n{"text_en": "Hi", "text_ar": "مرحبا"}\n```'])
    out = await _gateway(monkeypatch, fake).parse("message", "Word it.", [text_block("{}")], OwnerMessage,
                                                  offline=_OFFLINE)
    assert out.text_en == "Hi"


async def test_gateway_reply_off_schema_is_re_asked_once_then_unavailable(monkeypatch: Any) -> None:
    fixed = _FakeCompletions(['{"text_en": "Hi"}', '{"text_en": "Hi", "text_ar": "مرحبا"}'])
    assert (await _gateway(monkeypatch, fixed).parse("message", "Word it.", [text_block("{}")], OwnerMessage,
                                                     offline=_OFFLINE)).text_ar == "مرحبا"
    retry = fixed.calls[1]["messages"]
    assert retry[-2] == {"role": "assistant", "content": '{"text_en": "Hi"}'} and "schema" in retry[-1]["content"]

    never = _FakeCompletions(["not json", "still not json"])
    with pytest.raises(LLMUnavailableError):
        await _gateway(monkeypatch, never).parse("message", "Word it.", [text_block("{}")], OwnerMessage,
                                                 offline=_OFFLINE)
    assert len(never.calls) == 2


async def test_gateway_content_filter_is_a_refusal(monkeypatch: Any) -> None:
    from app.llm.client import LLMRefusalError

    fake = _FakeCompletions(["", ""], finish_reason="content_filter")
    with pytest.raises(LLMRefusalError):
        await _gateway(monkeypatch, fake).parse("message", "Word it.", [text_block("{}")], OwnerMessage,
                                                offline=_OFFLINE)


def test_gateway_without_its_settings_is_unavailable(monkeypatch: Any) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "LLM_PROVIDER", "openai_compatible")
    monkeypatch.setattr(get_settings(), "LLM_BASE_URL", None)
    with pytest.raises(LLMUnavailableError):
        LLMClient(mode="live")._api()


def test_fixture_key_follows_the_gateway_model(monkeypatch: Any) -> None:
    from app.config import get_settings

    args = ("message", "p", [text_block("x")], "OwnerMessage")
    base = request_hash(*args)
    monkeypatch.setattr(get_settings(), "LLM_MODEL", "claude-opus-5.5")
    assert request_hash(*args) != base
