"""Render uploaded Telegram-compatible HTML back into the sender's chat."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from dotenv import load_dotenv
from telegram import LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.ext import Application, ContextTypes, MessageHandler, filters

MAX_MESSAGE_LENGTH = 4096
POST_FILE_SUFFIX = ".tgpost.html"

logger = logging.getLogger(__name__)


class PostFileError(ValueError):
    """A user-facing problem with an uploaded post file."""


@dataclass(frozen=True)
class Settings:
    token: str
    allowed_user_id: int


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
    if not token or not raw_user_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN and ALLOWED_USER_ID must be set in .env")
    try:
        allowed_user_id = int(raw_user_id)
    except ValueError as exc:
        raise RuntimeError("ALLOWED_USER_ID must be a numeric Telegram user ID") from exc
    return Settings(token=token, allowed_user_id=allowed_user_id)


def document_filter(settings: Settings):
    """Build the only update filter the bot accepts."""
    return filters.Document.ALL & filters.User(user_id=settings.allowed_user_id)


async def render_post(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, allowed_user_id: int
) -> None:
    """Download a permitted post file and return it using Telegram HTML parsing."""
    message = update.effective_message
    user = update.effective_user
    if (
        message is None
        or message.document is None
        or not is_allowed_user(user.id if user else None, allowed_user_id)
        or not is_post_filename(message.document.file_name)
    ):
        return

    try:
        telegram_file = await message.document.get_file()
        html = read_post_html(bytes(await telegram_file.download_as_bytearray()))
        await message.reply_text(
            html,
            parse_mode=ParseMode.HTML,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )
    except PostFileError as exc:
        await message.reply_text(str(exc))
    except Exception:
        logger.exception("Could not render uploaded post")
        await message.reply_text("Telegram could not render this .tgpost.html file.")


def build_application(settings: Settings) -> Application:
    app = Application.builder().token(settings.token).build()
    app.add_handler(
        MessageHandler(
            document_filter(settings),
            lambda update, context: render_post(
                update, context, allowed_user_id=settings.allowed_user_id
            ),
        )
    )
    return app


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
    )
    settings = load_settings()
    logger.info("Starting Telegram post renderer for allowed user %s", settings.allowed_user_id)
    build_application(settings).run_polling()


if __name__ == "__main__":
    main()

