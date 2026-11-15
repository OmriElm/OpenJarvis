"""Deliver a generated morning digest to configured messaging channels.

Telegram is the supported channel. The bot token is read from
``[channel.telegram] bot_token`` or the ``TELEGRAM_BOT_TOKEN`` environment
variable. Tokens are never written to logs, including when a request fails.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

from openjarvis.core.config import JarvisConfig

logger = logging.getLogger(__name__)

_TELEGRAM = "telegram"


@dataclass(frozen=True, slots=True)
class DeliveryTarget:
    """One configured digest destination."""

    channel_type: str
    destination: str


@dataclass(frozen=True, slots=True)
class DeliveryReport:
    """How many destinations accepted, rejected, or were not attempted.

    ``skipped`` counts destinations that were not attempted because the
    digest text was empty. Failures are recorded per destination and do
    not raise.
    """

    sent: int = 0
    failed: int = 0
    skipped: int = 0


def parse_deliver_to(value: Any) -> tuple[list[DeliveryTarget], list[str]]:
    """Parse ``digest.deliver_to`` into targets and human-readable problems.

    Accepts a list of ``"channel:destination"`` strings or one
    comma-separated string. Problems are safe to log: they quote the
    config entry and never include a bot token.

    Parameters
    ----------
    value:
        The raw ``deliver_to`` setting.

    Returns
    -------
    tuple[list[DeliveryTarget], list[str]]
        Valid targets, then messages describing entries that were ignored.
    """
    if value is None:
        return [], []
    if isinstance(value, str):
        raw_items = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple)):
        raw_items = []
        for item in value:
            text = str(item).strip()
            if text:
                raw_items.append(text)
    else:
        return [], [
            "deliver_to must be a list of 'channel:destination' strings, "
            f"got {type(value).__name__}"
        ]

    targets: list[DeliveryTarget] = []
    problems: list[str] = []
    for item in raw_items:
        if ":" not in item:
            problems.append(
                f"ignoring deliver_to entry {item!r}; expected 'channel:destination'"
            )
            continue
        kind, destination = item.split(":", 1)
        kind = kind.strip().lower()
        destination = destination.strip()
        if not kind or not destination:
            problems.append(
                f"ignoring deliver_to entry {item!r}; expected 'channel:destination'"
            )
            continue
        targets.append(DeliveryTarget(channel_type=kind, destination=destination))
    return targets, problems


def deliver_digest_text(text: str, config: JarvisConfig) -> DeliveryReport:
    """Send *text* to every destination in ``config.digest.deliver_to``.

    An empty ``deliver_to`` is a no-op. Network errors, a missing token,
    and unknown channel types are counted in the returned report and are
    not raised, so delivery cannot fail digest generation.

    Long Telegram messages are split under the 4096 UTF-16-unit limit.
    The text is sent as plain text so generated Markdown and non-ASCII
    content (including Hebrew) are not rejected by Telegram's parser.

    Parameters
    ----------
    text:
        Digest body to send.
    config:
        Loaded configuration. The token is read from the channel section
        or ``TELEGRAM_BOT_TOKEN`` and is not logged.

    Returns
    -------
    DeliveryReport
        Counts of sent, failed, and skipped destinations.
    """
    raw = getattr(getattr(config, "digest", None), "deliver_to", None)
    targets, problems = parse_deliver_to(raw)
    for problem in problems:
        logger.warning("Digest delivery: %s", problem)
    if not targets:
        return DeliveryReport(failed=len(problems))

    if not str(text or "").strip():
        logger.info("Digest text is empty; skipping channel delivery")
        return DeliveryReport(skipped=len(targets), failed=len(problems))

    sent = 0
    failed = len(problems)
    for target in targets:
        if target.channel_type != _TELEGRAM:
            logger.warning(
                "Digest delivery does not support channel type %r yet",
                target.channel_type,
            )
            failed += 1
            continue
        if _send_telegram(target.destination, text, config):
            sent += 1
        else:
            failed += 1
    return DeliveryReport(sent=sent, failed=failed)


def deliver_scheduled_result(
    task: Any,
    text: str,
    config: JarvisConfig,
) -> DeliveryReport | None:
    """Deliver *text* when *task* is a morning-digest run.

    Other agents return ``None`` so this can be attached to the generic
    scheduler success callback without affecting them.

    Parameters
    ----------
    task:
        Scheduled task. Only ``agent == "morning_digest"`` is delivered.
    text:
        The task's result text.
    config:
        Configuration that names the destinations.

    Returns
    -------
    DeliveryReport or None
        The delivery report for a digest task, otherwise ``None``.
    """
    if getattr(task, "agent", "") != "morning_digest":
        return None
    return deliver_digest_text(text, config)


def _telegram_token(config: JarvisConfig) -> str:
    """Return the Telegram bot token without logging it."""
    configured = ""
    channel = getattr(config, "channel", None)
    telegram = getattr(channel, "telegram", None)
    if telegram is not None:
        configured = str(getattr(telegram, "bot_token", "") or "").strip()
    if configured:
        return configured
    return os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()


def _send_telegram(chat_id: str, text: str, config: JarvisConfig) -> bool:
    """Send *text* to one Telegram chat. Never log the bot token."""
    token = _telegram_token(config)
    if not token:
        logger.warning(
            "Digest delivery to telegram chat %s skipped: no bot token. "
            "Set [channel.telegram] bot_token or the TELEGRAM_BOT_TOKEN "
            "environment variable.",
            chat_id,
        )
        return False
    try:
        from openjarvis.channels.telegram import TelegramChannel

        # Plain text: digest prose contains Markdown characters and Hebrew
        # that Telegram's Markdown parser would reject.
        channel = TelegramChannel(bot_token=token, parse_mode="")
        ok = channel.send(chat_id, text)
    except Exception as exc:
        logger.warning(
            "Digest delivery to telegram chat %s failed (%s)",
            chat_id,
            type(exc).__name__,
        )
        return False
    if ok:
        logger.info("Delivered digest to telegram chat %s", chat_id)
        return True
    logger.warning("Digest delivery to telegram chat %s failed", chat_id)
    return False


__all__ = [
    "DeliveryReport",
    "DeliveryTarget",
    "deliver_digest_text",
    "deliver_scheduled_result",
    "parse_deliver_to",
]
