import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import TelegramError

from src.bot import (
    MAX_MESSAGE_LENGTH,
    PostFileError,
    Settings,
    configure_logging,
    handle_publish_callback,
    is_allowed_user,
    is_post_filename,
    read_post_html,
)


def test_recognizes_tgpost_html_filename():
    assert is_post_filename("launch.tgpost.html")


@pytest.mark.parametrize("filename", [None, "launch.html", "launch.tgpost.htm", "launch.TGPOST.HTML"])
def test_rejects_other_filenames(filename):
    assert not is_post_filename(filename)


def test_decodes_utf8_and_preserves_the_supplied_html():
    html = "<b>Привет</b> <code>exact source</code>"
    assert read_post_html(html.encode("utf-8")) == html


def test_rejects_invalid_utf8():
    with pytest.raises(PostFileError, match="UTF-8"):
        read_post_html(bytes([0xFF]))


def test_rejects_empty_file():
    with pytest.raises(PostFileError, match="empty"):
        read_post_html(b"  \n\t")


def test_rejects_oversized_post():
    with pytest.raises(PostFileError, match="too long"):
        read_post_html(b"x" * (MAX_MESSAGE_LENGTH + 1))


def test_allowed_user_filtering():
    assert is_allowed_user(42, 42)
    assert not is_allowed_user(43, 42)
    assert not is_allowed_user(None, 42)


def test_logging_does_not_emit_http_request_urls():
    configure_logging()

    assert logging.getLogger("httpx").getEffectiveLevel() > logging.INFO


def test_authorized_publish_sends_the_original_html_and_removes_the_button():
    html = "<b>Formatted</b> <tg-spoiler>secret</tg-spoiler>"
    pending_posts = {"post-1": html}
    bot = SimpleNamespace(send_message=AsyncMock())
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        data="publish:post-1",
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    settings = Settings(token="token", allowed_user_id=42, channel_id="@channel")

    asyncio.run(handle_publish_callback(query, bot, settings, pending_posts))

    assert bot.send_message.await_args.kwargs["chat_id"] == "@channel"
    assert bot.send_message.await_args.kwargs["text"] == html
    assert bot.send_message.await_args.kwargs["parse_mode"] == "HTML"
    query.answer.assert_awaited_once_with("Published.")
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
    assert pending_posts == {}


def test_publish_failure_keeps_the_preview_available():
    pending_posts = {"post-1": "<b>Formatted</b>"}
    bot = SimpleNamespace(send_message=AsyncMock(side_effect=TelegramError("not enough rights")))
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=42),
        data="publish:post-1",
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    settings = Settings(token="token", allowed_user_id=42, channel_id="@channel")

    asyncio.run(handle_publish_callback(query, bot, settings, pending_posts))

    assert pending_posts == {"post-1": "<b>Formatted</b>"}
    query.edit_message_reply_markup.assert_not_awaited()
    assert "Could not publish" in query.answer.await_args.args[0]


def test_unauthorized_user_cannot_publish():
    pending_posts = {"post-1": "<b>Formatted</b>"}
    bot = SimpleNamespace(send_message=AsyncMock())
    query = SimpleNamespace(
        from_user=SimpleNamespace(id=99),
        data="publish:post-1",
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )
    settings = Settings(token="token", allowed_user_id=42, channel_id="@channel")

    asyncio.run(handle_publish_callback(query, bot, settings, pending_posts))

    bot.send_message.assert_not_awaited()
    assert pending_posts == {"post-1": "<b>Formatted</b>"}
