"""Tests for channel message splitting."""

from __future__ import annotations

import pytest

from openjarvis.channels.splitting import split_message, utf16_len


def test_empty_string_splits_to_nothing() -> None:
    assert split_message("") == []


def test_short_message_is_one_piece() -> None:
    assert split_message("בוקר טוב") == ["בוקר טוב"]


def test_exact_limit_is_one_piece() -> None:
    text = "a" * 4096
    assert split_message(text) == [text]


def test_one_over_limit_preserves_every_character() -> None:
    text = "a" * 4097
    pieces = split_message(text)
    assert len(pieces) == 2
    assert "".join(pieces) == text
    assert all(utf16_len(piece) <= 4096 for piece in pieces)


def test_prefers_paragraph_then_line_breaks() -> None:
    text = ("a" * 100) + "\n\n" + ("b" * 100)
    pieces = split_message(text, limit=150)
    assert pieces == [("a" * 100) + "\n\n", "b" * 100]

    lined = ("a" * 20) + "\n" + ("b" * 20)
    assert split_message(lined, limit=30) == [("a" * 20) + "\n", "b" * 20]


def test_hard_splits_a_token_with_no_whitespace() -> None:
    pieces = split_message("abcdef", limit=4)
    assert pieces == ["abcd", "ef"]
    assert "".join(pieces) == "abcdef"


def test_emoji_counts_as_two_utf16_units() -> None:
    emoji = "🙂"
    assert utf16_len(emoji) == 2
    pieces = split_message(emoji * 3, limit=2)
    assert pieces == [emoji, emoji, emoji]
    assert all(utf16_len(piece) <= 2 for piece in pieces)


def test_hebrew_is_not_corrupted() -> None:
    text = "שלום " * 1000
    pieces = split_message(text, limit=50)
    assert "".join(pieces) == text
    assert all(utf16_len(piece) <= 50 for piece in pieces)
    assert all(piece for piece in pieces)


def test_limit_must_be_positive() -> None:
    with pytest.raises(ValueError):
        split_message("hi", limit=0)
