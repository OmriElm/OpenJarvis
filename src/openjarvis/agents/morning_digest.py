"""Morning Digest Agent — synthesizes a daily briefing from multiple sources.

Thin orchestrator that delegates to digest_collect (data fetching),
the LLM (narrative synthesis), and text_to_speech (audio generation).
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional

from openjarvis.agents._stubs import AgentContext, AgentResult, ToolUsingAgent
from openjarvis.agents.digest_store import DigestArtifact, DigestStore
from openjarvis.core.paths import get_config_dir
from openjarvis.core.registry import AgentRegistry
from openjarvis.core.types import Message, Role, ToolCall

logger = logging.getLogger(__name__)

STYLE_SPOKEN = "spoken"
STYLE_TEXT = "text"
_DIGEST_STYLES = frozenset({STYLE_SPOKEN, STYLE_TEXT})
DEFAULT_MAX_WORDS = 200

_SECTION_PROMPTS = {
    "messages": "MESSAGES — Prioritize provided messages or tasks needing action.",
    "calendar": "CALENDAR — Cover only provided upcoming events.",
    "health": "HEALTH — Describe only supported trends; omit raw measurements.",
    "world": "WORLD — Summarize only provided world items.",
    "music": "MUSIC — Summarize only provided listening information.",
}

# Markers that make a phone message look like unparsed Markdown, plus emoji
# that a speech engine would read aloud or skip awkwardly.
_MARKDOWN_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_LIST_MARKER = re.compile(r"^\s*[-*•]\s+", re.MULTILINE)
_EMPHASIS = re.compile(r"\*{1,2}([^*]+)\*{1,2}")
_EMOJI = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff\ufe0f]+",
    flags=re.UNICODE,
)


def _load_persona(persona_name: str) -> str:
    """Load a persona prompt file by name.

    The file is read as UTF-8 so a non-English persona (for example Hebrew)
    is not decoded with the Windows locale encoding.
    """
    search_paths = [
        Path("configs/openjarvis/prompts/personas") / f"{persona_name}.md",
        get_config_dir() / "prompts" / "personas" / f"{persona_name}.md",
    ]
    for p in search_paths:
        if p.exists():
            return p.read_text(encoding="utf-8")
    return ""


def normalize_digest_style(value: Any) -> str:
    """Return ``spoken`` or ``text``.

    Anything else is logged and treated as ``spoken``, which is the
    historical briefing. The logged value is truncated so a huge string
    cannot flood the log.

    Parameters
    ----------
    value:
        Raw ``digest.style`` setting.

    Returns
    -------
    str
        ``spoken`` or ``text``.
    """
    if not isinstance(value, str):
        logger.warning(
            "Invalid digest style (%s); using spoken",
            type(value).__name__,
        )
        return STYLE_SPOKEN
    style = value.strip().lower()
    if style in _DIGEST_STYLES:
        return style
    shown = style if len(style) <= 40 else style[:37] + "..."
    logger.warning("Invalid digest style %r; using spoken", shown)
    return STYLE_SPOKEN


def normalize_max_words(value: Any) -> int:
    """Return a positive word budget, or the default of 200.

    Parameters
    ----------
    value:
        Raw ``digest.max_words`` setting.

    Returns
    -------
    int
        A word limit of at least 1.
    """
    if isinstance(value, bool) or value is None:
        logger.warning(
            "Invalid digest max_words (%s); using %d",
            type(value).__name__,
            DEFAULT_MAX_WORDS,
        )
        return DEFAULT_MAX_WORDS
    if isinstance(value, str):
        value = value.strip()
    try:
        words = int(value)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid digest max_words (%s); using %d",
            type(value).__name__,
            DEFAULT_MAX_WORDS,
        )
        return DEFAULT_MAX_WORDS
    if words < 1:
        logger.warning(
            "digest max_words must be positive (got %d); using %d",
            words,
            DEFAULT_MAX_WORDS,
        )
        return DEFAULT_MAX_WORDS
    return words


def _as_bool(value: Any) -> bool:
    """Coerce a config flag without treating the string ``false`` as true."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def normalize_unread_hours(value: Any) -> int:
    """Return the unread window in hours.

    ``0`` means no date cutoff. A missing or negative value uses 168
    (seven days), so an unread message from a few days ago is still
    included and a year-old unread pile is not.

    Parameters
    ----------
    value:
        Raw ``digest.messages.unread_hours_back``.

    Returns
    -------
    int
        A non-negative hour count.
    """
    if isinstance(value, bool) or value is None:
        return 168
    if isinstance(value, str):
        value = value.strip()
    try:
        hours = int(value)
    except (TypeError, ValueError):
        logger.warning("Invalid digest.messages.unread_hours_back; using 168")
        return 168
    if hours < 0:
        logger.warning(
            "digest.messages.unread_hours_back must be >= 0 (got %d); using 168",
            hours,
        )
        return 168
    return hours


def _section_block(sections: List[str]) -> str:
    """Return the configured-section lines, in first-seen order."""
    ordered = dict.fromkeys(
        str(section).strip().casefold() for section in sections if str(section).strip()
    )
    lines = [
        f"- {_SECTION_PROMPTS.get(section, section.upper())}" for section in ordered
    ]
    return "\n".join(lines) if lines else "- None"


def digest_format_instructions(
    style: str,
    max_words: int,
    *,
    include_messages: bool,
) -> str:
    """Return the format half of the digest system prompt.

    ``spoken`` is a single prose briefing for text-to-speech. ``text`` is a
    scannable plain-text message. Both keep the zero-hallucination rules.
    The text layout tells the model to write headings in the persona's
    language so an English instruction block does not flip a Hebrew persona
    back to English.

    Parameters
    ----------
    style:
        Already normalized ``spoken`` or ``text``.
    max_words:
        Positive word budget for the whole briefing.
    include_messages:
        When true, the text layout also requires the mail groups.

    Returns
    -------
    str
        Format instructions, including the absolute rules.
    """
    shared = (
        "ABSOLUTE RULES (violations are unacceptable):\n"
        "- ONLY facts from the data. Zero hallucination.\n"
        "- NEVER mention disconnected or unavailable sources.\n"
        "- NEVER invent personal context or claim, offer, or suggest actions.\n"
        "- Acknowledge every source that returned data, even briefly.\n"
        "- If the collected data reports a mailbox was checked and has no "
        "unread emails, say that there are no new emails, in the persona's "
        "language. That statement is required. Do not treat the mailbox as "
        "absent and do not invent emails.\n"
        f"- STRICT LIMIT: {max_words} words. Be concise."
    )
    if style != STYLE_TEXT:
        return (
            "Produce a concise spoken briefing in decreasing order of importance. "
            "Cover only the configured sections listed above and only when the "
            "collected data supports them. Silently omit absent data and sources.\n\n"
            "Open briefly with the honorific and end after the last supported item. "
            "Do not add conversational offers or personal asides.\n\n"
            f"{shared}\n"
            "- No markdown, emojis, bullets, or headers."
        )
    mail = ""
    if include_messages:
        mail = (
            "\nWhen messages contain email or chat items, group those items "
            "under three headings, still in the persona's language: items that "
            "need attention or a reply; quieter updates, including deliveries, "
            "copying tracking numbers exactly as written in the data; and "
            "promotions plus mail the content itself shows is spam or phishing. "
            "Omit an empty group. Do not call something spam or phishing unless "
            "the collected text supports that. Do not invent a tracking number.\n"
        )
    return (
        "Produce a scannable plain-text briefing for a phone screen, in "
        "decreasing order of importance. Cover only the configured sections "
        "listed above and only when the collected data supports them. "
        "Silently omit absent data and sources.\n\n"
        "Write every word, including headings, in the persona's language. "
        "These instructions are in English so the layout is unambiguous. "
        "Do not switch to English unless the persona itself is written in "
        "English. The English section keys listed above (MESSAGES, CALENDAR, "
        "and so on) describe what to cover. They are not heading text. Do not "
        "copy them into the briefing.\n\n"
        "A persona written for speech may forbid emoji, headings, and "
        "bullets. Ignore that format ban for this message only. Keep the "
        "persona's language, tone, and honorific.\n\n"
        "Do not use Markdown. No asterisks, underscores, hash marks, "
        "backticks, or bracketed links. Those characters would show up "
        "literally in the chat. A heading is one short line that starts "
        "with a single emoji and then words in the persona's language. "
        "Under it, one short line per item, each starting with a hyphen "
        "and a space. Do not merge unrelated items into a paragraph.\n"
        f"{mail}\n"
        "Open briefly with the honorific and end after the last supported "
        "item. Do not add conversational offers or personal asides.\n\n"
        f"{shared}"
    )


def digest_user_suffix(style: str, max_words: int) -> str:
    """Return the closing instructions for the user message.

    Parameters
    ----------
    style:
        Already normalized style.
    max_words:
        Positive word budget.

    Returns
    -------
    str
        Instructions that follow the collected-data block.
    """
    shared = (
        "Use configured sections only. Omit missing data and sources. "
        "Do not add personal context or activities. "
        "Use the honorific no more than three times and keep the "
        f"briefing under {max_words} words. "
        "A mailbox check that reports zero unread emails is not missing "
        "data; say there are no new emails in the persona's language."
    )
    if style == STYLE_TEXT:
        return (
            f"{shared} Use the plain-text layout from the system prompt, "
            "in the persona's language. No Markdown syntax."
        )
    return f"{shared} No markdown, emojis, bullets, or headers."


def strip_for_speech(text: str) -> str:
    """Remove Markdown markers and emoji so speech stays prose.

    Parameters
    ----------
    text:
        Briefing text, possibly with headings or list markers.

    Returns
    -------
    str
        Text safe to send to a speech engine.
    """
    cleaned = _MARKDOWN_HEADING.sub("", text or "")
    cleaned = _LIST_MARKER.sub("", cleaned)
    cleaned = _EMPHASIS.sub(r"\1", cleaned)
    cleaned = _EMOJI.sub("", cleaned)
    return cleaned.strip()


def spoken_rewrite_instructions(max_words: int) -> str:
    """Instructions for turning a text digest into a spoken script.

    The script is for audio only. It may not add facts that are absent
    from the text briefing, and it stays in that briefing's language.

    Parameters
    ----------
    max_words:
        Positive word budget.

    Returns
    -------
    str
        System prompt for the rewrite call.
    """
    return (
        "Rewrite the briefing below so it can be spoken aloud. "
        "Use only facts, names, and numbers that already appear in it. "
        "Do not add items, and do not drop a tracking number that is present. "
        "Write in the same language as the briefing. Do not translate it. "
        "No markdown, emojis, bullets, or headers. "
        f"Stay under {max_words} words. "
        "Return only the spoken briefing."
    )


@AgentRegistry.register("morning_digest")
class MorningDigestAgent(ToolUsingAgent):
    """Pre-compute a daily digest from configured data sources."""

    agent_id = "morning_digest"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        # Extract digest-specific kwargs before passing to parent
        self._persona = kwargs.pop("persona", "jarvis")
        self._sections = kwargs.pop(
            "sections", ["messages", "calendar", "health", "world"]
        )
        self._section_sources = kwargs.pop("section_sources", {})
        self._timezone = kwargs.pop("timezone", "America/Los_Angeles")
        self._voice_id = kwargs.pop("voice_id", "")
        self._voice_speed = kwargs.pop("voice_speed", 1.0)
        self._tts_backend = kwargs.pop("tts_backend", "cartesia")
        self._digest_store_path = kwargs.pop("digest_store_path", "")
        self._honorific = kwargs.pop("honorific", "sir")
        self._style = normalize_digest_style(kwargs.pop("style", STYLE_SPOKEN))
        self._max_words = normalize_max_words(
            kwargs.pop("max_words", DEFAULT_MAX_WORDS)
        )
        self._messages_unread_only = _as_bool(kwargs.pop("messages_unread_only", False))
        self._messages_unread_hours_back = normalize_unread_hours(
            kwargs.pop("messages_unread_hours_back", 168)
        )
        super().__init__(*args, **kwargs)

    def _collect_params(self, sources: List[str]) -> dict[str, Any]:
        """Arguments for ``digest_collect``.

        Non-mail sources keep the 24-hour digest window. When unread mail
        is requested, email connectors get that flag and their own window
        (``0`` means every unread message).
        """
        payload: dict[str, Any] = {"sources": sources, "hours_back": 24}
        if self._messages_unread_only:
            payload["email_unread_only"] = True
            payload["email_hours_back"] = self._messages_unread_hours_back
        return payload

    def _configured_section_names(self) -> List[str]:
        """Return configured section names, lower-cased, first-seen order."""
        return list(
            dict.fromkeys(
                str(section).strip().casefold()
                for section in self._sections
                if str(section).strip()
            )
        )

    def _build_system_prompt(self) -> str:
        """Assemble the system prompt from persona, sections, and style.

        ``style=spoken`` (the default) is a prose script for speech.
        ``style=text`` is a scannable plain-text message. The persona's
        language still wins: English layout rules must not pull a Hebrew
        persona back into English.

        Returns
        -------
        str
            The system prompt for the briefing generation.
        """
        persona_text = _load_persona(self._persona)
        now = datetime.now()
        honorific = getattr(self, "_honorific", "sir")
        sections = self._configured_section_names()
        section_block = _section_block(sections)
        format_block = digest_format_instructions(
            self._style,
            self._max_words,
            include_messages="messages" in sections,
        )
        return (
            f"{persona_text}\n\n"
            f"Today is {now.strftime('%A, %B %d, %Y')}. "
            f"The time is {now.strftime('%I:%M %p')} in {self._timezone}.\n"
            f"The user's preferred honorific is: {honorific}\n\n"
            "You receive structured data from the user's connected services. "
            "The data has ALREADY been collected — it appears in the user "
            "message. You do NOT fetch anything yourself.\n\n"
            f"CONFIGURED SECTIONS:\n{section_block}\n\n"
            f"{format_block}"
        )

    def _user_content(self, collected_data: str) -> str:
        """Wrap collected data with the style-specific closing rules."""
        return (
            "The following collected data is the only factual evidence for "
            f"the briefing:\n\n<collected_data>\n{collected_data}\n"
            "</collected_data>\n\n"
            f"{digest_user_suffix(self._style, self._max_words)}"
        )

    def _tts_text(self, narrative: str) -> str:
        """Return the text that should be spoken.

        Spoken style is the narrative with Markdown markers removed.
        Text style is rewritten into speech first. A failed or empty
        rewrite falls back to the marker-stripped narrative so audio is
        still produced.

        Parameters
        ----------
        narrative:
            The briefing that will be stored and delivered.

        Returns
        -------
        str
            Text for the speech engine.
        """
        if self._style == STYLE_TEXT and narrative.strip():
            spoken = self._rewrite_for_speech(narrative)
            if spoken.strip():
                return strip_for_speech(spoken)
            logger.warning(
                "Spoken rewrite for the digest was empty; "
                "reading the text briefing aloud"
            )
        return strip_for_speech(narrative)

    def _rewrite_for_speech(self, narrative: str) -> str:
        """Rewrite *narrative* into speech without adding facts.

        Exceptions are swallowed. A speech failure must not discard the
        text briefing that channels will deliver.

        Parameters
        ----------
        narrative:
            The text-style briefing.

        Returns
        -------
        str
            The spoken script, or an empty string when the call fails.
        """
        messages = [
            Message(
                role=Role.SYSTEM,
                content=spoken_rewrite_instructions(self._max_words),
            ),
            Message(role=Role.USER, content=narrative),
        ]
        try:
            result = self._generate(messages)
        except Exception as exc:
            logger.warning(
                "Spoken rewrite for the digest failed (%s)",
                type(exc).__name__,
            )
            return ""
        if not isinstance(result, dict):
            return ""
        return self._strip_think_tags(str(result.get("content", "") or ""))

    def _resolve_sources(self) -> List[str]:
        """Get the list of connector IDs to query."""
        default_source_map = {
            "messages": [
                "gmail",
                "slack",
                "google_tasks",
                "imessage",
                "github_notifications",
            ],
            "calendar": ["gcalendar"],
            "health": ["oura", "apple_health"],
            "world": ["weather", "hackernews", "news_rss"],
            "music": ["spotify", "apple_music"],
        }
        sources = set()
        for section in self._sections:
            section_sources = self._section_sources.get(
                section, default_source_map.get(section, [])
            )
            sources.update(section_sources)
        return list(sources)

    def run(
        self,
        input: str,
        context: Optional[AgentContext] = None,
        **kwargs: Any,
    ) -> AgentResult:
        self._emit_turn_start(input)

        # Step 1: Collect data from connectors
        sources = self._resolve_sources()
        collect_call = ToolCall(
            id="digest-collect-1",
            name="digest_collect",
            arguments=json.dumps(self._collect_params(sources)),
        )
        collect_result = self._executor.execute(collect_call)
        collected_data = collect_result.content

        # Step 2: Synthesize narrative via LLM
        system_prompt = self._build_system_prompt()
        messages = [
            Message(role=Role.SYSTEM, content=system_prompt),
            Message(role=Role.USER, content=self._user_content(collected_data)),
        ]

        result = self._generate(messages)
        narrative = self._strip_think_tags(result.get("content", ""))

        # Step 2b: Self-evaluate and optionally regenerate
        quality_score = 0.0
        evaluator_feedback = ""
        try:
            from openjarvis.agents.digest_evaluator import DigestEvaluator

            evaluator = DigestEvaluator(self._engine, self._model)
            quality_score, evaluator_feedback = evaluator.evaluate(
                collected_data, narrative
            )

            if quality_score < 7.0 and evaluator_feedback:
                # Regenerate with feedback
                messages.append(
                    Message(
                        role=Role.USER,
                        content=(
                            f"Your briefing scored {quality_score:.1f}/10. "
                            f"Feedback: {evaluator_feedback}\n"
                            "Revise the briefing. Keep the same language and the "
                            "format required above. Stay under "
                            f"{self._max_words} words. Do not add facts that are "
                            "not in the collected data."
                        ),
                    )
                )
                result = self._generate(messages)
                narrative = self._strip_think_tags(result.get("content", ""))
        except Exception:  # noqa: BLE001
            pass  # Evaluator failure shouldn't block digest delivery

        # Step 3: Generate audio via TTS.
        # style=text stores and delivers the scannable message. Speech still
        # gets a spoken script so it does not read emoji headings aloud.
        # That script is a rewrite of the message, not a second look at the
        # raw data, so it cannot introduce facts the text omitted. If the
        # rewrite fails, the text is read after markers are stripped.
        tts_text = self._tts_text(narrative)

        output_dir = str(get_config_dir() / "digests")
        tts_call = ToolCall(
            id="digest-tts-1",
            name="text_to_speech",
            arguments=json.dumps(
                {
                    "text": tts_text,
                    "voice_id": self._voice_id,
                    "backend": self._tts_backend,
                    "speed": self._voice_speed,
                    "output_dir": output_dir,
                }
            ),
        )
        tts_result = self._executor.execute(tts_call)
        audio_path = (
            tts_result.metadata.get("audio_path", "") if tts_result.success else ""
        )

        # Step 4: Store the artifact
        artifact = DigestArtifact(
            text=narrative,
            audio_path=Path(audio_path) if audio_path else Path(""),
            sections={},
            sources_used=sources,
            generated_at=datetime.now(),
            model_used=self._model,
            voice_used=self._voice_id,
            quality_score=quality_score,
            evaluator_feedback=evaluator_feedback,
        )

        store = DigestStore(db_path=self._digest_store_path)
        store.save(artifact)
        store.close()

        self._emit_turn_end(turns=1)
        return AgentResult(
            content=narrative,
            tool_results=[collect_result, tts_result],
            turns=1,
            metadata={
                "audio_path": audio_path,
                "sources_used": sources,
            },
        )
