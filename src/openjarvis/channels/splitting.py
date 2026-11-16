"""Split outbound text so every piece fits a messaging-channel limit.

Telegram's ``sendMessage`` limit is 4096 UTF-16 code units, not Unicode
code points. An emoji is one Python character and two UTF-16 units, so a
split that counts ``len(text)`` can be rejected. Hebrew and ASCII are one
unit each and are unaffected.
"""

from __future__ import annotations

# Telegram Bot API: sendMessage text is at most 4096 UTF-16 code units.
TELEGRAM_TEXT_LIMIT = 4096


def utf16_len(text: str) -> int:
    """Return the number of UTF-16 code units in *text*."""
    return len(text.encode("utf-16-le")) // 2


def split_message(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split *text* into pieces of at most *limit* UTF-16 code units.

    Breaks are chosen at the last paragraph boundary in the window, then
    the last line break, then the last space. A token longer than *limit*
    is cut on a code-point boundary. Joining the pieces reproduces *text*
    exactly, including Hebrew and other non-ASCII characters. An empty
    string produces no pieces.

    Parameters
    ----------
    text:
        The message to split.
    limit:
        Maximum UTF-16 length of each piece. Must be at least 1. A limit
        of 1 cannot hold an emoji (two units); the piece then contains
        that single character so the split still makes progress.

    Returns
    -------
    list[str]
        The ordered pieces. Empty when *text* is empty.

    Raises
    ------
    ValueError
        If *limit* is less than 1.
    """
    if limit < 1:
        raise ValueError(f"limit must be positive, got {limit}")
    if text == "":
        return []

    pieces: list[str] = []
    remaining = text
    while remaining:
        if utf16_len(remaining) <= limit:
            pieces.append(remaining)
            break
        cut = _cut_index(remaining, limit)
        pieces.append(remaining[:cut])
        remaining = remaining[cut:]
    return pieces


def _cut_index(text: str, limit: int) -> int:
    """Return a positive code-point index whose prefix fits in *limit*."""
    units = 0
    end = 0
    for index, char in enumerate(text):
        char_units = 2 if ord(char) > 0xFFFF else 1
        if units + char_units > limit:
            break
        units += char_units
        end = index + 1
    if end <= 0:
        # The first character alone is wider than the limit. Emit it so
        # the caller cannot loop forever; only a limit below 2 hits this.
        return 1
    window = text[:end]
    for separator in ("\n\n", "\n", " "):
        found = window.rfind(separator)
        # A cut at 0 would emit an empty piece and stall the caller.
        if found > 0:
            return found + len(separator)
    return end


__all__ = ["TELEGRAM_TEXT_LIMIT", "split_message", "utf16_len"]
