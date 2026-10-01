"""LLM client settings: the model, room for thinking, and replay fixtures tied to model and effort."""

from __future__ import annotations

from typing import Any

from app.llm import client
from app.llm.client import request_hash, text_block


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
