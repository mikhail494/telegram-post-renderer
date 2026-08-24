import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import TelegramError

from src.bot import (
    MAX_CAPTION_LENGTH,
    MAX_MESSAGE_LENGTH,
    DraftStore,
    PendingImage,
    PostFileError,
    Settings,
    capture_photo,
    configure_logging,
    handle_publish_callback,
    image_payload,
    is_allowed_user,
    is_post_filename,
    is_supported_image_document,
    read_post_html,
    rendered_html_text_length,
)


SETTINGS = Settings(token="token", allowed_user_id=42, channel_id="@channel")


def make_query(user_id: int, post_id: str):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        data=f"publish:{post_id}",
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
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


@pytest.mark.parametrize(
    ("file_name", "mime_type"),
    [("image.png", "image/png"), ("image.jpeg", "image/jpeg"), ("image.jpg", "image/jpeg"), ("image.webp", "image/webp")],
)
def test_recognizes_supported_image_documents(file_name, mime_type):
    document = SimpleNamespace(file_name=file_name, mime_type=mime_type)

    assert is_supported_image_document(document)


def test_rejects_mismatched_image_document_type():
    document = SimpleNamespace(file_name="image.gif", mime_type="image/jpeg")

    assert not is_supported_image_document(document)


def test_document_image_is_reuploaded_as_a_photo_payload():
    telegram_file = SimpleNamespace(download_as_bytearray=AsyncMock(return_value=bytearray(b"image")))
    bot = SimpleNamespace(get_file=AsyncMock(return_value=telegram_file))
    image = PendingImage(file_id="document-file", file_name="image.png")

    payload = asyncio.run(image_payload(bot, image))

    bot.get_file.assert_awaited_once_with("document-file")
    assert payload.filename == "image.png"


def test_counts_rendered_html_text_for_caption_limit():
    html = "<b>" + ("x" * MAX_CAPTION_LENGTH) + "</b>"

    assert len(html) > MAX_CAPTION_LENGTH
    assert rendered_html_text_length(html) == MAX_CAPTION_LENGTH


def test_allowed_user_photo_becomes_the_pending_image():
    state = DraftStore()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_message=SimpleNamespace(
            photo=[SimpleNamespace(file_id="small"), SimpleNamespace(file_id="large")]
        ),
    )

    asyncio.run(capture_photo(update, SETTINGS, state))

    assert state.pending_image_file_id == "large"


def test_unauthorized_photo_is_ignored():
    state = DraftStore()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=99),
        effective_message=SimpleNamespace(photo=[SimpleNamespace(file_id="image")]),
    )

    asyncio.run(capture_photo(update, SETTINGS, state))

    assert state.pending_image_file_id is None


def test_newer_pending_image_replaces_the_previous_one():
    state = DraftStore()

    state.set_pending_image("first")
    state.set_pending_image("second")

    assert state.pending_image_file_id == "second"


def test_post_binds_and_consumes_the_pending_image():
    state = DraftStore()
    state.set_pending_image("image")

    post_id = state.create_post("<b>Post</b>")

    assert state.posts[post_id].image_file_id == "image"
    assert state.pending_image_file_id is None


def test_text_only_post_has_no_image():
    state = DraftStore()

    post_id = state.create_post("<b>Post</b>")

    assert state.posts[post_id].image_file_id is None


def test_text_only_publish_sends_the_original_html_and_removes_the_button():
    html = "<b>Formatted</b> <tg-spoiler>secret</tg-spoiler>"
    state = DraftStore()
    post_id = state.create_post(html)
    bot = SimpleNamespace(send_message=AsyncMock())
    query = make_query(42, post_id)

    asyncio.run(handle_publish_callback(query, bot, SETTINGS, state))

    assert bot.send_message.await_args.kwargs["chat_id"] == "@channel"
    assert bot.send_message.await_args.kwargs["text"] == html
    assert bot.send_message.await_args.kwargs["parse_mode"] == "HTML"
    query.answer.assert_awaited_once_with("Published.")
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
    assert state.posts == {}


def test_image_short_post_publishes_html_caption_and_clears_draft():
    html = "<b>Formatted</b>"
    state = DraftStore()
    state.set_pending_image("image")
    post_id = state.create_post(html)
    bot = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())
    query = make_query(42, post_id)

    asyncio.run(handle_publish_callback(query, bot, SETTINGS, state))

    assert bot.send_photo.await_args.kwargs["photo"] == "image"
    assert bot.send_photo.await_args.kwargs["caption"] == html
    assert bot.send_photo.await_args.kwargs["parse_mode"] == "HTML"
    bot.send_message.assert_not_awaited()
    assert state.posts == {}


def test_image_long_post_publishes_image_then_full_html_text():
    html = "x" * (MAX_CAPTION_LENGTH + 1)
    state = DraftStore()
    state.set_pending_image("image")
    post_id = state.create_post(html)
    bot = SimpleNamespace(send_photo=AsyncMock(), send_message=AsyncMock())
    query = make_query(42, post_id)

    asyncio.run(handle_publish_callback(query, bot, SETTINGS, state))

    bot.send_photo.assert_awaited_once_with(chat_id="@channel", photo="image")
    assert bot.send_message.await_args.kwargs["text"] == html
    assert bot.send_message.await_args.kwargs["parse_mode"] == "HTML"
    assert state.posts == {}


def test_long_post_retry_does_not_publish_the_image_twice_after_text_failure():
    html = "x" * (MAX_CAPTION_LENGTH + 1)
    state = DraftStore()
    state.set_pending_image("image")
    post_id = state.create_post(html)
    bot = SimpleNamespace(
        send_photo=AsyncMock(), send_message=AsyncMock(side_effect=TelegramError("temporary failure"))
    )
    first_query = make_query(42, post_id)

    asyncio.run(handle_publish_callback(first_query, bot, SETTINGS, state))

    assert state.posts[post_id].image_published
    bot.send_photo.assert_awaited_once()
    bot.send_message.side_effect = None
    retry_query = make_query(42, post_id)
    asyncio.run(handle_publish_callback(retry_query, bot, SETTINGS, state))

    bot.send_photo.assert_awaited_once()
    assert bot.send_message.await_count == 2
    assert state.posts == {}


def test_image_publish_failure_keeps_the_draft_for_retry():
    state = DraftStore()
    state.set_pending_image("image")
    post_id = state.create_post("<b>Post</b>")
    bot = SimpleNamespace(send_photo=AsyncMock(side_effect=TelegramError("not enough rights")))
    query = make_query(42, post_id)

    asyncio.run(handle_publish_callback(query, bot, SETTINGS, state))

    assert post_id in state.posts
    query.edit_message_reply_markup.assert_not_awaited()
    assert "Could not publish" in query.answer.await_args.args[0]


def test_unauthorized_user_cannot_publish():
    state = DraftStore()
    post_id = state.create_post("<b>Formatted</b>")
    bot = SimpleNamespace(send_message=AsyncMock())
    query = make_query(99, post_id)

    asyncio.run(handle_publish_callback(query, bot, SETTINGS, state))

    bot.send_message.assert_not_awaited()
    assert post_id in state.posts
