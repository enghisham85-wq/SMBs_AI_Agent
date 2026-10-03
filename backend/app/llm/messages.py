"""Owner-message wording. The model only words the message; code owns the numbers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.core.i18n import normalize_digits
from app.llm.client import LLMRefusalError, LLMUnavailableError, MissingFixtureError, get_llm, text_block
from app.llm.schemas import OwnerMessage

_PROMPT = (Path(__file__).parent / "prompts" / "owner_message.md").read_text(encoding="utf-8")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER.findall(normalize_digits(text))}


def numbers_are_grounded(message: OwnerMessage, facts: dict[str, Any]) -> bool:
    """Every number in the message must appear in the facts (after digit normalisation)."""
    fact_numbers = _numbers(json.dumps(facts, ensure_ascii=False, default=str))
    return _numbers(message.text_en) <= fact_numbers and _numbers(message.text_ar) <= fact_numbers


async def compose(facts: dict[str, Any], fallback_en: str, fallback_ar: str) -> OwnerMessage:
    """Word a message from computed facts; fall back to the template if the model adds numbers."""
    fallback = OwnerMessage(text_en=fallback_en[:280], text_ar=fallback_ar[:280])
    try:
        msg = await get_llm().parse(
            "message",
            _PROMPT,
            [text_block("Facts (JSON):\n" + json.dumps(facts, ensure_ascii=False, default=str))],
            OwnerMessage,
            offline=lambda: fallback,
        )
    except (LLMRefusalError, LLMUnavailableError, MissingFixtureError):
        return fallback
    return msg if numbers_are_grounded(msg, facts) else fallback
