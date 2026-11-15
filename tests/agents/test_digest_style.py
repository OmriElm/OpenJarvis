"""Digest style: spoken prose by default, scannable text when configured."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from openjarvis.agents.digest_store import DigestStore
from openjarvis.agents.morning_digest import (
    DEFAULT_MAX_WORDS,
    MorningDigestAgent,
    normalize_digest_style,
    normalize_max_words,
    strip_for_speech,
)
from openjarvis.core.config import DigestConfig
from openjarvis.core.types import ToolCall, ToolResult


def _agent(**kwargs: object) -> MorningDigestAgent:
    return MorningDigestAgent(MagicMock(), "test-model", tools=[], **kwargs)


def test_defaults_stay_spoken() -> None:
    cfg = DigestConfig()
    assert cfg.style == "spoken"
    assert cfg.max_words == DEFAULT_MAX_WORDS
    agent = _agent()
    prompt = agent._build_system_prompt()
    user = agent._user_content("data")
    assert agent._style == "spoken"
    assert agent._max_words == 200
    assert "No markdown, emojis, bullets, or headers." in prompt
    assert "STRICT LIMIT: 200 words." in prompt
    assert "under 200 words" in user
    assert "need attention" not in prompt


def test_unknown_style_and_word_budget_fall_back(caplog) -> None:
    with caplog.at_level("WARNING"):
        assert normalize_digest_style("Markdown") == "spoken"
        assert normalize_digest_style(None) == "spoken"
        assert normalize_max_words(0) == 200
        assert normalize_max_words("lots") == 200
        assert normalize_max_words(True) == 200
    assert normalize_digest_style(" Text ") == "text"
    assert normalize_max_words(" 80 ") == 80
    agent = _agent(style="nope", max_words=-5)
    assert agent._style == "spoken"
    assert agent._max_words == 200


def test_text_style_is_plain_text_and_stays_in_the_persona_language(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "openjarvis.agents.morning_digest._load_persona",
        lambda _name: "אתה עוזר. כתוב בעברית בלבד.",
    )
    agent = _agent(style="text", max_words=350, sections=["messages", "calendar"])
    prompt = agent._build_system_prompt()
    user = agent._user_content("מייל")

    assert "כתוב בעברית בלבד." in prompt
    assert "Do not switch to English" in prompt
    assert "persona's language" in prompt
    assert "need attention" in prompt
    assert "tracking numbers" in prompt
    assert "spam or phishing" in prompt
    assert "No asterisks" in prompt
    assert "STRICT LIMIT: 350 words." in prompt
    assert "No markdown, emojis, bullets, or headers." not in prompt
    assert "under 350 words" in user
    assert "No Markdown syntax." in user
    # The instruction text must not demonstrate the syntax it forbids.
    assert "**" not in prompt
    assert "`" not in prompt


def test_text_style_omits_mail_groups_without_a_messages_section() -> None:
    agent = _agent(style="text", sections=["world"])
    prompt = agent._build_system_prompt()
    assert "tracking numbers" not in prompt
    assert "world —" in prompt.casefold()


def test_strip_for_speech_keeps_hebrew_and_tracking_numbers() -> None:
    cleaned = strip_for_speech("📦 משלוח\n- מעקב 1Z999\n**דחוף**")
    assert "משלוח" in cleaned
    assert "1Z999" in cleaned
    assert "דחוף" in cleaned
    assert "📦" not in cleaned
    assert "*" not in cleaned
    assert "- " not in cleaned


def test_text_style_delivers_the_list_and_speaks_a_rewrite(tmp_path) -> None:
    text = "📦 משלוח\n- מעקב 1Z999"
    spoken = "החבילה בדרך, מספר המעקב 1Z999."
    engine = MagicMock()
    engine.generate.side_effect = [
        {"content": text, "finish_reason": "stop", "usage": {}},
        {"content": spoken, "finish_reason": "stop", "usage": {}},
    ]
    collected = ToolResult(
        tool_name="digest_collect",
        content="=== MESSAGES ===\ntracking 1Z999\n",
        success=True,
    )
    audio = ToolResult(
        tool_name="text_to_speech",
        content="audio",
        success=True,
        metadata={"audio_path": str(tmp_path / "digest.mp3")},
    )
    agent = _agent(
        style="text",
        sections=["messages"],
        section_sources={"messages": ["gmail_imap"]},
        digest_store_path=str(tmp_path / "digest.db"),
    )
    agent._engine = engine
    calls: list[ToolCall] = []

    def execute(call: ToolCall) -> ToolResult:
        calls.append(call)
        if call.name == "digest_collect":
            return collected
        return audio

    agent._executor.execute = execute
    result = agent.run("Generate morning digest")

    assert result.content == text
    assert engine.generate.call_count == 2
    rewrite = engine.generate.call_args_list[1].args[0]
    assert rewrite[1].text == text
    assert "same language" in rewrite[0].text
    assert "Do not add items" in rewrite[0].text
    tts_text = json.loads(calls[1].arguments)["text"]
    assert tts_text == spoken
    assert "📦" not in tts_text
    stored = DigestStore(str(tmp_path / "digest.db"))
    try:
        latest = stored.get_latest()
        assert latest is not None
        assert latest.text == text
    finally:
        stored.close()


def test_text_style_reads_stripped_text_when_the_rewrite_fails(tmp_path) -> None:
    text = "📦 משלוח\n- מעקב 1Z999"
    engine = MagicMock()
    engine.generate.side_effect = [
        {"content": text, "finish_reason": "stop", "usage": {}},
        RuntimeError("engine down"),
    ]
    collected = ToolResult(
        tool_name="digest_collect",
        content="tracking 1Z999",
        success=True,
    )
    audio = ToolResult(
        tool_name="text_to_speech",
        content="audio",
        success=True,
        metadata={"audio_path": ""},
    )
    agent = _agent(
        style="text",
        sections=["messages"],
        digest_store_path=str(tmp_path / "digest.db"),
    )
    agent._engine = engine
    calls: list[ToolCall] = []

    def execute(call: ToolCall) -> ToolResult:
        calls.append(call)
        if call.name == "digest_collect":
            return collected
        return audio

    agent._executor.execute = execute
    result = agent.run("Generate morning digest")

    assert result.content == text
    tts_text = json.loads(calls[1].arguments)["text"]
    assert "1Z999" in tts_text
    assert "משלוח" in tts_text
    assert "📦" not in tts_text
    assert "- " not in tts_text
