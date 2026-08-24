"""Render uploaded Telegram-compatible HTML and publish approved previews."""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field
from html.parser import HTMLParser
from io import BytesIO

from dotenv import load_dotenv
from telegram import (
    Bot,
    CallbackQuery,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, ContextTypes, MessageHandler, filters

MAX_MESSAGE_LENGTH = 4096
MAX_CAPTION_LENGTH = 1024
POST_FILE_SUFFIX = ".tgpost.html"
PUBLISH_CALLBACK_PREFIX = "publish:"
IMAGE_DOCUMENT_SUFFIXES = (".png", ".jpeg", ".jpg", ".webp")
IMAGE_DOCUMENT_MIME_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})

logger = logging.getLogger(__name__)


class PostFileError(ValueError):
    """A user-facing problem with an uploaded post file."""


@dataclass(frozen=True)
class Settings:
    token: str
    allowed_user_id: int
    channel_id: str


@dataclass
class PendingImage:
    """A Telegram image reference, with a name when it originated as a document."""

    file_id: str
    file_name: str | None = None


@dataclass
class PendingPost:
    """One preview's immutable HTML and optionally associated Telegram image."""

    html: str
    image: PendingImage | None
    image_published: bool = False

    @property
    def image_file_id(self) -> str | None:
        return self.image.file_id if self.image else None


@dataclass
class DraftStore:
    """In-memory draft state; pending work is intentionally lost on restart."""

    pending_image: PendingImage | None = None
    posts: dict[str, PendingPost] = field(default_factory=dict)
    active_post_id: str | None = None

    @property
    def pending_image_file_id(self) -> str | None:
        return self.pending_image.file_id if self.pending_image else None

    @property
    def active_post(self) -> PendingPost | None:
        return self.posts.get(self.active_post_id) if self.active_post_id else None

    def set_pending_image(self, file_id: str, file_name: str | None = None) -> bool:
        """Attach an image to the active draft, or save it for the next one."""
        image = PendingImage(file_id=file_id, file_name=file_name)
        post = self.active_post
        if post:
            post.image = image
            post.image_published = False
            return True
        self.pending_image = image
        return False

    def create_post(self, html: str) -> str:
        post_id = secrets.token_urlsafe(12)
        self.posts.clear()
        self.posts[post_id] = PendingPost(html, self.pending_image)
        self.active_post_id = post_id
        self.pending_image = None
        return post_id

    def remove_post(self, post_id: str) -> None:
        self.posts.pop(post_id, None)
        if self.active_post_id == post_id:
            self.active_post_id = None
            self.pending_image = None


def is_post_filename(filename: str | None) -> bool:
    """Return whether a document name is the exact supported input type."""
    return bool(filename and filename.endswith(POST_FILE_SUFFIX))


def is_supported_image_document(document: Document | None) -> bool:
    """Return whether a document is one of the supported single-post image types."""
    if document is None:
        return False
    filename = (document.file_name or "").lower()
    return filename.endswith(IMAGE_DOCUMENT_SUFFIXES) and (
        document.mime_type is None or document.mime_type in IMAGE_DOCUMENT_MIME_TYPES
    )


class TelegramHTMLTextLengthParser(HTMLParser):
    """Count Telegram HTML's rendered text without changing the HTML source."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text_length = 0

    def handle_data(self, data: str) -> None:
        self.text_length += len(data)


def rendered_html_text_length(html: str) -> int:
    """Return the rendered text length used to choose Telegram's caption path."""
    parser = TelegramHTMLTextLengthParser()
    parser.feed(html)
    parser.close()
    return parser.text_length


async def image_payload(bot: Bot, image: PendingImage) -> str | InputFile:
    """Return an image usable with send_photo without changing a document file ID's type."""
    if image.file_name is None:
        return image.file_id
    telegram_file = await bot.get_file(image.file_id)
    data = bytes(await telegram_file.download_as_bytearray())
    return InputFile(BytesIO(data), filename=image.file_name)


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
    """Build the document filter used for post files and supported images."""
    return filters.Document.ALL & filters.User(user_id=settings.allowed_user_id)


async def capture_photo(
    update: Update, settings: Settings, drafts: DraftStore, bot: Bot | None = None
) -> None:
    """Remember the largest authorized Telegram photo for the next post."""
    message = update.effective_message
    user = update.effective_user
    if (
        message is None
        or not message.photo
        or not is_allowed_user(user.id if user else None, settings.allowed_user_id)
    ):
        return
    attached_to_post = drafts.set_pending_image(message.photo[-1].file_id)
    if attached_to_post and bot and drafts.active_post and drafts.active_post.image:
        await message.reply_photo(await image_payload(bot, drafts.active_post.image))


async def handle_document(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    settings: Settings,
    drafts: DraftStore,
) -> None:
    """Accept a supported image document or render a post file from one input seam."""
    message = update.effective_message
    user = update.effective_user
    document = message.document if message else None
    if (
        document is None
        or not is_allowed_user(user.id if user else None, settings.allowed_user_id)
    ):
        return
    if is_supported_image_document(document):
        attached_to_post = drafts.set_pending_image(document.file_id, document.file_name)
        if attached_to_post and drafts.active_post and drafts.active_post.image:
            await message.reply_photo(await image_payload(context.bot, drafts.active_post.image))
        return
    await render_post(update, context, settings=settings, drafts=drafts)


async def render_post(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    settings: Settings,
    drafts: DraftStore,
) -> None:
    """Render an authorized post preview and bind any pending image to it."""
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
        post_id = drafts.create_post(html)
        post = drafts.posts[post_id]
        if post.image:
            await message.reply_photo(await image_payload(context.bot, post.image))
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
    query: CallbackQuery, bot: Bot, settings: Settings, drafts: DraftStore
) -> None:
    """Publish a pending HTML preview after an authorized button press."""
    if not is_allowed_user(query.from_user.id if query.from_user else None, settings.allowed_user_id):
        await query.answer("Not authorized.", show_alert=True)
        return

    post_id = (query.data or "").removeprefix(PUBLISH_CALLBACK_PREFIX)
    if post_id != drafts.active_post_id:
        await query.answer("This preview is no longer available.", show_alert=True)
        return
    post = drafts.active_post
    if not post or not post_id:
        await query.answer("This preview is no longer available.", show_alert=True)
        return

    try:
        if post.image and rendered_html_text_length(post.html) <= MAX_CAPTION_LENGTH:
            await bot.send_photo(
                chat_id=settings.channel_id,
                photo=await image_payload(bot, post.image),
                caption=post.html,
                parse_mode=ParseMode.HTML,
            )
        elif post.image:
            if not post.image_published:
                await bot.send_photo(
                    chat_id=settings.channel_id, photo=await image_payload(bot, post.image)
                )
                post.image_published = True
            await bot.send_message(
                chat_id=settings.channel_id,
                text=post.html,
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        else:
            await bot.send_message(
                chat_id=settings.channel_id,
                text=post.html,
                parse_mode=ParseMode.HTML,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
    except TelegramError as exc:
        logger.warning("Telegram rejected publish request: %s", exc)
        await query.answer("Could not publish: Telegram rejected the post.", show_alert=True)
        return

    drafts.remove_post(post_id)
    await query.answer("Published.")
    await query.edit_message_reply_markup(reply_markup=None)


def build_application(settings: Settings) -> Application:
    app = Application.builder().token(settings.token).build()
    drafts = DraftStore()
    app.add_handler(
        MessageHandler(
            document_filter(settings),
            lambda update, context: handle_document(update, context, settings=settings, drafts=drafts),
        )
    )
    app.add_handler(
        MessageHandler(
            filters.PHOTO & filters.User(user_id=settings.allowed_user_id),
            lambda update, context: capture_photo(update, settings, drafts, context.bot),
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            lambda update, context: handle_publish_callback(
                update.callback_query, context.bot, settings, drafts
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
