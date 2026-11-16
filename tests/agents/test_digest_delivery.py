"""Tests for morning-digest channel delivery. Network calls are mocked."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

from openjarvis.agents.digest_delivery import (
    deliver_digest_text,
    deliver_scheduled_result,
    parse_deliver_to,
)
from openjarvis.channels.splitting import utf16_len
from openjarvis.core.config import JarvisConfig
from openjarvis.scheduler.scheduler import ScheduledTask

_TOKEN = "123456:SUPER-SECRET-TOKEN"


def _config(*destinations: str, token: str = _TOKEN) -> JarvisConfig:
    cfg = JarvisConfig()
    cfg.digest.deliver_to = list(destinations)
    cfg.channel.telegram.bot_token = token
    return cfg


def _ok() -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.text = "ok"
    return response


class TestParseDeliverTo:
    def test_list_and_comma_string(self) -> None:
        targets, problems = parse_deliver_to(
            ["telegram:123", " telegram:-100 "],
        )
        assert problems == []
        assert [(t.channel_type, t.destination) for t in targets] == [
            ("telegram", "123"),
            ("telegram", "-100"),
        ]

        targets, problems = parse_deliver_to("telegram:1, telegram:2")
        assert problems == []
        assert len(targets) == 2

    def test_malformed_entries_are_reported(self) -> None:
        targets, problems = parse_deliver_to(["nocolon", "telegram:"])
        assert targets == []
        assert len(problems) == 2


class TestDeliver:
    def test_empty_config_does_not_call_the_network(self) -> None:
        with patch("httpx.post") as post:
            report = deliver_digest_text("hello", _config())
        post.assert_not_called()
        assert report.sent == 0
        assert report.failed == 0

    def test_hebrew_is_split_and_sent_as_plain_text(self) -> None:
        text = "בוקר טוב " + ("ש" * 5000)
        captured: list[dict] = []

        def fake_post(url, json, timeout):
            assert _TOKEN in url
            captured.append(json)
            return _ok()

        with patch("httpx.post", side_effect=fake_post):
            report = deliver_digest_text(text, _config("telegram:42"))

        assert report.sent == 1
        assert report.failed == 0
        bodies = [item["text"] for item in captured]
        assert "".join(bodies) == text
        assert len(bodies) >= 2
        assert all(utf16_len(part) <= 4096 for part in bodies)
        assert all("parse_mode" not in item for item in captured)
        assert all(item["chat_id"] == "42" for item in captured)

    def test_token_comes_from_the_environment(self, monkeypatch) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token-value")
        cfg = _config("telegram:7", token="")

        def fake_post(url, json, timeout):
            assert "env-token-value" in url
            assert _TOKEN not in url
            return _ok()

        with patch("httpx.post", side_effect=fake_post):
            report = deliver_digest_text("שלום", cfg)
        assert report.sent == 1

    def test_config_token_wins_over_the_environment(self, monkeypatch) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env-token-value")

        def fake_post(url, json, timeout):
            assert _TOKEN in url
            assert "env-token-value" not in url
            return _ok()

        with patch("httpx.post", side_effect=fake_post):
            report = deliver_digest_text("hello", _config("telegram:7"))
        assert report.sent == 1

    def test_missing_token_is_a_failure_without_a_request(self, monkeypatch) -> None:
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        with patch("httpx.post") as post:
            report = deliver_digest_text("hello", _config("telegram:7", token=""))
        post.assert_not_called()
        assert report.failed == 1
        assert report.sent == 0

    def test_network_error_does_not_raise_or_log_the_token(self, caplog) -> None:
        def fake_post(url, json, timeout):
            raise httpx.ConnectError("connection refused")

        with (
            caplog.at_level("WARNING"),
            patch("httpx.post", side_effect=fake_post),
        ):
            report = deliver_digest_text("שלום", _config("telegram:7"))

        assert report.failed == 1
        assert report.sent == 0
        assert _TOKEN not in caplog.text
        assert "bot" + _TOKEN not in caplog.text

    def test_http_error_body_containing_the_token_is_not_logged(self, caplog) -> None:
        response = MagicMock()
        response.status_code = 401
        response.text = f"unauthorized {_TOKEN}"

        with (
            caplog.at_level("DEBUG"),
            patch("httpx.post", return_value=response),
        ):
            report = deliver_digest_text("hello", _config("telegram:7"))

        assert report.failed == 1
        assert _TOKEN not in caplog.text

    def test_one_destination_failing_does_not_skip_the_next(self) -> None:
        bad = MagicMock()
        bad.status_code = 500
        bad.text = "nope"
        calls: list[str] = []

        def fake_post(url, json, timeout):
            calls.append(json["chat_id"])
            if json["chat_id"] == "1":
                return bad
            return _ok()

        with patch("httpx.post", side_effect=fake_post):
            report = deliver_digest_text(
                "hello",
                _config("telegram:1", "telegram:2"),
            )
        assert calls == ["1", "2"]
        assert report.sent == 1
        assert report.failed == 1

    def test_unsupported_channel_is_a_failure(self) -> None:
        with patch("httpx.post") as post:
            report = deliver_digest_text("hello", _config("slack:C123"))
        post.assert_not_called()
        assert report.failed == 1

    def test_empty_text_is_skipped(self) -> None:
        with patch("httpx.post") as post:
            report = deliver_digest_text("   ", _config("telegram:1"))
        post.assert_not_called()
        assert report.skipped == 1
        assert report.sent == 0

    def test_scheduled_callback_ignores_other_agents(self) -> None:
        task = ScheduledTask(
            id="t",
            prompt="p",
            schedule_type="once",
            schedule_value="2026-01-01T00:00:00+00:00",
            agent="simple",
        )
        with patch("httpx.post") as post:
            assert deliver_scheduled_result(task, "hi", _config("telegram:1")) is None
        post.assert_not_called()

    def test_scheduled_callback_delivers_morning_digest(self) -> None:
        task = ScheduledTask(
            id="t",
            prompt="p",
            schedule_type="cron",
            schedule_value="0 8 * * *",
            agent="morning_digest",
        )
        with patch("httpx.post", return_value=_ok()):
            report = deliver_scheduled_result(task, "בוקר טוב", _config("telegram:9"))
        assert report is not None
        assert report.sent == 1


def test_parse_rejects_non_list_values() -> None:
    targets, problems = parse_deliver_to(3)
    assert targets == []
    assert problems
    assert "int" in problems[0]
