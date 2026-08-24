"""Render uploaded Telegram-compatible HTML back into the sender's chat."""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass

from dotenv import load_dotenv
from telegram import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

MAX_MESSAGE_LENGTH = 4096
POST_FILE_SUFFIX = ".tgpost.html"
PUBLISH_CALLBACK_PREFIX = "publish:"

logger = logging.getLogger(__name__)


class PostFileError(ValueError):
    """A user-facing problem with an uploaded post file."""


@dataclass(frozen=True)
class Settings:
    token: str
    allowed_user_id: int
    channel_id: str


def is_post_filename(filename: str | None) -> bool:
    """Return whether a document name is the exact supported input type."""
    return bool(filename and filename.endswith(POST_FILE_SUFFIX))


def is_allowed_user(user_id: int | None, allowed_user_id: int) -> bool:
    return user_id == allowed_user_id


def read_post_html(data: bytes) -> str:
    """Decode and apply the minimal pre-send constraints to source HTML."""
    try:
        html = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PostFileError("The file must be UTF-8 text.") from exc

    if not html.strip():
        raise PostFileError("The .tgpost.html file is empty.")
    if len(html) > MAX_MESSAGE_LENGTH:
        raise PostFileError("The .tgpost.html file is too long; please shorten it.")
    return html


def load_settings() -> Settings:
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    raw_user_id = os.environ.get("ALLOWED_USER_ID", "")
    channel_id = os.environ.get("TELEGRAM_CHANNEL_ID", "")
    if not token or not raw_user_id or not channel_id:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN, ALLOWED_USER_ID, and TELEGRAM_CHANNEL_ID must be set in .env"
        )
    try:
        allowed_user_id = int(raw_user_id)
    except ValueError as exc:
        raise RuntimeError("ALLOWED_USER_ID must be a numeric Telegram user ID") from exc
    return Settings(token=token, allowed_user_id=allowed_user_id, channel_id=channel_id)


def document_filter(settings: Settings):
    """Build the only update filter the bot accepts."""
    return filters.Document.ALL & filters.User(user_id=settings.allowed_user_id)


async def render_post(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    settings: Settings,
    pending_posts: dict[str, str],
) -> None:
    """Download a permitted post file and return it using Telegram HTML parsing."""
    message = update.effective_message
    user = update.effective_user
    if (
        message is None
        or message.document is None
        or not is_allowed_user(user.id if user else None, settings.allowed_user_id)
        or not is_post_filename(message.document.file_name)
    ):
        return

    try:
        telegram_file = await message.document.get_file()
        html = read_post_html(bytes(await telegram_file.download_as_bytearray()))
        post_id = secrets.token_urlsafe(12)
        pending_posts[post_id] = html
        await message.reply_text(
            html,
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Publish", callback_data=f"{PUBLISH_CALLBACK_PREFIX}{post_id}")]]
            ),
        )
    except PostFileError as exc:
        await message.reply_text(str(exc))
    except Exception:
        logger.exception("Could not render uploaded post")
        await message.reply_text("Telegram could not render this .tgpost.html file.")


async def handle_publish_callback(
    query: CallbackQuery, bot, settings: Settings, pending_posts: dict[str, str]
) -> None:
    """Publish a pending HTML preview after an authorized button press."""
    if not is_allowed_user(query.from_user.id if query.from_user else None, settings.allowed_user_id):
        await query.answer("Not authorized.", show_alert=True)
        return

    post_id = (query.data or "").removeprefix(PUBLISH_CALLBACK_PREFIX)
    html = pending_posts.get(post_id)
    if not html:
        await query.answer("This preview is no longer available.", show_alert=True)
        return

    try:
        await bot.send_message(
            chat_id=settings.channel_id,
            text=html,
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except TelegramError as exc:
        logger.warning("Telegram rejected publish request: %s", exc)
        await query.answer("Could not publish: Telegram rejected the post.", show_alert=True)
        return

    pending_posts.pop(post_id, None)
    await query.answer("Published.")
    await query.edit_message_reply_markup(reply_markup=None)


def build_application(settings: Settings) -> Application:
    app = Application.builder().token(settings.token).build()
    pending_posts: dict[str, str] = {}
    app.add_handler(
        MessageHandler(
            document_filter(settings),
            lambda update, context: render_post(
                update, context, settings=settings, pending_posts=pending_posts
            ),
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            lambda update, context: handle_publish_callback(
                update.callback_query, context.bot, settings, pending_posts
            ),
            pattern=f"^{PUBLISH_CALLBACK_PREFIX}",
        )
    )
    return app


def configure_logging() -> None:
    """Configure application logs without exposing Bot API credentials."""
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    configure_logging()
    settings = load_settings()
    logger.info("Starting Telegram post renderer for allowed user %s", settings.allowed_user_id)
    build_application(settings).run_polling()


if __name__ == "__main__":
    main()

