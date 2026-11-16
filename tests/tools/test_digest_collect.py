"""Tests for the digest_collect tool."""

from __future__ import annotations

import importlib
from datetime import datetime
from unittest.mock import MagicMock, patch

from openjarvis.connectors._stubs import Document
from openjarvis.core.registry import ConnectorRegistry, ToolRegistry


def test_digest_collect_registered():
    from openjarvis.tools import digest_collect

    # The registry fixture clears entries, while Python caches imported modules.
    if not ToolRegistry.contains("digest_collect"):
        importlib.reload(digest_collect)
    assert ToolRegistry.get("digest_collect") is digest_collect.DigestCollectTool


def test_digest_collect_executes():
    from openjarvis.tools.digest_collect import DigestCollectTool

    tool = DigestCollectTool()

    mock_docs = [
        Document(
            doc_id="test-1",
            source="gmail",
            doc_type="email",
            content="Meeting at 3pm",
            title="Team standup",
            author="alice@example.com",
            timestamp=datetime(2026, 4, 1, 10, 0),
        )
    ]

    mock_connector = MagicMock()
    mock_connector.return_value.is_connected.return_value = True
    mock_connector.return_value.sync.return_value = mock_docs

    with patch.object(ConnectorRegistry, "contains", return_value=True):
        with patch.object(ConnectorRegistry, "get", return_value=mock_connector):
            result = tool.execute(sources=["gmail"], hours_back=24)

    assert result.success is True
    assert "=== MESSAGES ===" in result.content
    assert "[gmail id=test-1] From: alice@example.com" in result.content
    assert "Team standup" in result.content
    assert result.metadata["total_items"] == 1


def test_digest_collect_unread_email_is_server_side_and_reports_empty():
    from datetime import timedelta

    from openjarvis.tools.digest_collect import DigestCollectTool

    tool = DigestCollectTool()
    gmail = MagicMock()
    gmail.return_value.is_connected.return_value = True
    gmail.return_value.sync.return_value = []
    imap = MagicMock()
    imap.return_value.is_connected.return_value = True
    imap.return_value.sync.return_value = []

    def get_connector(source: str):
        return {"gmail": gmail, "gmail_imap": imap}[source]

    with (
        patch.object(ConnectorRegistry, "contains", return_value=True),
        patch.object(ConnectorRegistry, "get", side_effect=get_connector),
    ):
        result = tool.execute(
            sources=["gmail", "gmail_imap"],
            hours_back=24,
            email_unread_only=True,
            email_hours_back=168,
        )

    assert "=== MESSAGES ===" in result.content
    assert result.content.count("MAILBOX CHECKED: 0 unread emails") == 2
    assert "in the last 168 hours" in result.content
    assert "no new emails" in result.content
    gmail_kwargs = gmail.return_value.sync.call_args.kwargs
    assert gmail_kwargs["query_extra"] == "is:unread"
    assert "unread_only" not in gmail_kwargs
    gmail_since = gmail_kwargs["since"]
    assert abs((datetime.now() - gmail_since) - timedelta(hours=168)) < timedelta(
        minutes=1
    )
    imap_kwargs = imap.return_value.sync.call_args.kwargs
    assert imap_kwargs["unread_only"] is True
    assert imap_kwargs["since"] is not None


def test_digest_collect_unread_hours_zero_has_no_date_cutoff():
    from openjarvis.tools.digest_collect import DigestCollectTool

    tool = DigestCollectTool()
    imap = MagicMock()
    imap.return_value.is_connected.return_value = True
    imap.return_value.sync.return_value = []

    with (
        patch.object(ConnectorRegistry, "contains", return_value=True),
        patch.object(ConnectorRegistry, "get", return_value=imap),
    ):
        tool.execute(
            sources=["gmail_imap"],
            email_unread_only=True,
            email_hours_back=0,
        )

    assert imap.return_value.sync.call_args.kwargs["since"] is None
    assert imap.return_value.sync.call_args.kwargs["unread_only"] is True


def test_digest_collect_missing_connector():
    from openjarvis.tools.digest_collect import DigestCollectTool

    tool = DigestCollectTool()

    with patch.object(ConnectorRegistry, "contains", return_value=False):
        result = tool.execute(sources=["nonexistent"])

    assert result.success is True  # Partial success
    assert "not available" in result.content


def test_digest_collect_shows_calendar_start_time():
    from openjarvis.tools.digest_collect import DigestCollectTool

    tool = DigestCollectTool()

    mock_docs = [
        Document(
            doc_id="evt-1",
            source="gcalendar",
            doc_type="event",
            content="When: 2026-04-01T09:05:00 – 2026-04-01T10:35:00",
            title="Design review",
            timestamp=datetime(2026, 4, 1, 9, 5),
        )
    ]

    mock_connector = MagicMock()
    mock_connector.return_value.is_connected.return_value = True
    mock_connector.return_value.sync.return_value = mock_docs

    with patch.object(ConnectorRegistry, "contains", return_value=True):
        with patch.object(ConnectorRegistry, "get", return_value=mock_connector):
            result = tool.execute(sources=["gcalendar"], hours_back=24)

    expected = "[gcalendar id=evt-1] 9:05 AM — Design review (1 hour 30 min)"
    assert expected in result.content


def test_oura_line_without_day_uses_timestamp_date():
    from openjarvis.tools.digest_collect import _format_doc

    doc = Document(
        doc_id="sleep-1",
        source="oura",
        doc_type="sleep",
        content='{"score": 82}',
        timestamp=datetime(2026, 4, 1, 7, 0),
        metadata={"data_type": "sleep"},
    )

    assert _format_doc("oura", doc).startswith("[oura] Sleep — April 1: score 82")
