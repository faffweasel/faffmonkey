"""Telegram channel via python-telegram-bot. Contrib extension."""

import asyncio
import io
import json
import logging
import os
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
)

from faffmonkey.runtime.loop import SLASH_COMMANDS
from faffmonkey.types import InboundMessage, OutboundMessage

logger = logging.getLogger(__name__)

TELEGRAM_MAX_LENGTH = 4096


def _split_message(text: str, limit: int = TELEGRAM_MAX_LENGTH) -> list[str]:
    """Split at the API's 4096-character limit, preferring line then word
    breaks.
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut > 0:
            chunks.append(text[:cut])
            text = text[cut + 1:]
            continue
        cut = text.rfind(" ", 0, limit)
        if cut > 0:
            chunks.append(text[:cut])
            text = text[cut + 1:]
            continue
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks


def _group_id(update: Update) -> str | None:
    """The chat id for a group or supergroup, None for a private chat.

    Group conversations get their own session: everyone in the group reads
    the reply, so it must not draw on the owner's direct conversation.
    """
    chat = update.effective_chat
    if chat is None or chat.type not in ("group", "supergroup"):
        return None
    return str(chat.id)


def _normalise_command(text: str) -> str:
    """In groups Telegram sends commands as /help@botname; the runtime
    matches on the bare command. /start is what the Start button sends on
    first contact, and the runtime has no such command, so the first thing
    a new chat saw was "Unknown command"."""
    if not text.startswith("/"):
        return text
    head, sep, rest = text.partition(" ")
    head = head.split("@", 1)[0]
    if head == "/start":
        head = "/help"
    return head + sep + rest


class TelegramChannel:
    channel_id: str = "telegram"

    def __init__(
        self,
        allowed_users: list[str] | None = None,
        workspace: Path | None = None,
    ) -> None:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN not set")
        self._token = token
        self._allowed_users: set[str] = set(allowed_users or [])
        self._inbox = Path(workspace / "shared" / "inbox") if workspace else None
        # state/ is the workspace's sibling, the same derivation the skill
        # scripts use. The chat id belongs there and not in workspace/, which
        # is the agent's world.
        self._state_path = (
            workspace.parent / "state" / "channel-telegram.json" if workspace else None
        )
        self._queue: queue.Queue[InboundMessage] = queue.Queue()
        self._app: Application | None = None
        self._last_chat_id: int | None = self._load_chat_id()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._closed = False

    def is_allowed(self, sender_id: str) -> bool:
        if not self._allowed_users:
            return False
        return sender_id in self._allowed_users

    async def _capture_loop(self, app: Application) -> None:
        self._loop = asyncio.get_running_loop()
        # The "/" menu in Telegram shows whatever the bot last registered,
        # which for a reused bot is some other project's commands.
        try:
            await app.bot.set_my_commands(
                [(cmd.lstrip("/"), desc) for cmd, desc in SLASH_COMMANDS.items()]
            )
        except Exception:
            logger.warning("could not register the slash-command menu", exc_info=True)
        self._ready.set()

    def _poll_forever(self) -> None:
        # run_polling asks asyncio.get_event_loop() for a loop, and a worker
        # thread has none unless one is set first.
        asyncio.set_event_loop(asyncio.new_event_loop())
        try:
            # stop_signals=None: run_polling is on a worker thread, where
            # signal handlers cannot be registered.
            self._app.run_polling(drop_pending_updates=True, stop_signals=None)
        except Exception:
            logger.exception("telegram polling stopped")
        finally:
            self._closed = True
            self._ready.set()

    def start(self) -> None:
        """Start polling on a worker thread and return.

        run_polling never returns until shutdown, so it cannot share a
        thread with the caller's receive loop.
        """
        self._app = (
            Application.builder()
            .token(self._token)
            .post_init(self._capture_loop)
            .build()
        )
        # Commands included: the runtime handles /help, /status and the
        # rest, so ~filters.COMMAND must not filter them out here.
        self._app.add_handler(MessageHandler(filters.TEXT, self._on_text))
        self._app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, self._on_voice))
        self._app.add_handler(MessageHandler(filters.PHOTO, self._on_photo))
        self._app.add_handler(MessageHandler(filters.Document.ALL, self._on_document))
        self._thread = threading.Thread(
            target=self._poll_forever, daemon=True, name="telegram-polling",
        )
        self._thread.start()
        # Wait for the event loop to exist before returning, so an immediate
        # send() has somewhere to go.
        if not self._ready.wait(timeout=30):
            logger.warning("telegram did not become ready within 30s")

    def stop(self) -> None:
        self._closed = True
        if self._app is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(self._app.stop_running)
        if self._thread is not None:
            self._thread.join(timeout=10)

    def is_closed(self) -> bool:
        return self._closed

    def receive(self) -> InboundMessage | None:
        try:
            return self._queue.get(timeout=1.0)
        except queue.Empty:
            return None

    def poll(self) -> InboundMessage | None:
        """Non-blocking variant of receive, used while a goal is active."""
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None

    def _submit(self, coro, label: str) -> None:
        try:
            future = asyncio.run_coroutine_threadsafe(coro, self._loop)
            future.result(timeout=30)
        except Exception as e:
            logger.warning("telegram %s failed: %s", label, e)

    def send(self, message: OutboundMessage) -> None:
        if self._app is None or self._loop is None:
            return
        # A reply goes back to the room it answers. Anything else, which is
        # a cron announcement, goes to the owner's private chat, never to the
        # last group the owner spoke in.
        chat_id = int(message.group_id) if message.group_id else self._last_chat_id
        if chat_id is None:
            return
        for chunk in _split_message(message.text):
            self._submit(
                self._app.bot.send_message(chat_id=chat_id, text=chunk),
                "send_message",
            )
        if message.audio is not None:
            buf = io.BytesIO(message.audio)
            if message.audio_mime == "audio/ogg":
                self._submit(
                    self._app.bot.send_voice(chat_id=chat_id, voice=buf),
                    "send_voice",
                )
            else:
                self._submit(
                    self._app.bot.send_audio(
                        chat_id=chat_id, audio=buf, filename="reply.wav",
                    ),
                    "send_audio",
                )
        for path in message.attachments:
            with open(path, "rb") as f:
                self._submit(
                    self._app.bot.send_document(chat_id=chat_id, document=f),
                    "send_document",
                )

    def _load_chat_id(self) -> int | None:
        """The chat this bot last replied in, from a previous process.

        Without this, a cron job firing after a container restart and before
        the operator has said anything found no chat id, returned from send()
        without raising, and the scheduler recorded the run as delivered. The
        briefing was in the session and had gone nowhere.
        """
        if self._state_path is None or not self._state_path.is_file():
            return None
        try:
            data = json.loads(self._state_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("cannot read %s: %s", self._state_path, e)
            return None
        chat_id = data.get("last_chat_id") if isinstance(data, dict) else None
        return chat_id if isinstance(chat_id, int) else None

    def _remember_chat_id(self, chat_id: int) -> None:
        if chat_id == self._last_chat_id:
            return
        self._last_chat_id = chat_id
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"last_chat_id": chat_id}) + "\n")
            os.replace(tmp, self._state_path)
        except OSError as e:
            logger.warning("cannot persist telegram chat id: %s", e)

    def _accept(self, update: Update) -> str | None:
        """Validate the update and return the sender id, or None to drop it.

        The allow-list is checked BEFORE the originating chat is recorded,
        so a stranger's message cannot redirect the owner's replies and cron
        announcements to the stranger's chat.

        A chat id is only overwritten when the update actually carries one,
        so an update without an effective_chat cannot blank a good value.
        """
        if update.effective_message is None or update.effective_user is None:
            return None
        sender_id = str(update.effective_user.id)
        if not self.is_allowed(sender_id):
            logger.debug("dropping telegram update from %s", sender_id)
            return None
        if update.effective_chat is not None and _group_id(update) is None:
            self._remember_chat_id(update.effective_chat.id)
        return sender_id

    async def _on_text(self, update: Update, context: object) -> None:
        sender_id = self._accept(update)
        if sender_id is None:
            return
        self._queue.put(InboundMessage(
            sender_id=sender_id,
            text=_normalise_command(update.effective_message.text or ""),
            channel_id=self.channel_id,
            timestamp=datetime.now(timezone.utc),
            group_id=_group_id(update),
        ))

    async def _on_voice(self, update: Update, context: object) -> None:
        sender_id = self._accept(update)
        if sender_id is None:
            return
        voice = update.effective_message.voice or update.effective_message.audio
        if voice is None:
            return
        file = await voice.get_file()
        data = await file.download_as_bytearray()
        self._queue.put(InboundMessage(
            sender_id=sender_id,
            text="",
            channel_id=self.channel_id,
            timestamp=datetime.now(timezone.utc),
            group_id=_group_id(update),
            audio=bytes(data),
            audio_mime="audio/ogg",
        ))

    async def _on_photo(self, update: Update, context: object) -> None:
        sender_id = self._accept(update)
        if sender_id is None:
            return
        if not update.effective_message.photo:
            return
        photo = update.effective_message.photo[-1]
        file = await photo.get_file()
        images: list[Path] = []
        if self._inbox is not None:
            self._inbox.mkdir(parents=True, exist_ok=True)
            dest = self._inbox / f"photo_{file.file_unique_id}.jpg"
            await file.download_to_drive(str(dest))
            logger.info("saved photo to %s", dest)
            images.append(dest)
        self._queue.put(InboundMessage(
            sender_id=sender_id,
            # Metadata, not attributed speech. "What is this?" was persisted
            # as the operator's own words, so the history recorded a question
            # nobody asked. Discord already words it this way, and loop.py
            # carries the same fix for missing transcriptions.
            text=update.effective_message.caption or "(sent a photo)",
            channel_id=self.channel_id,
            timestamp=datetime.now(timezone.utc),
            group_id=_group_id(update),
            images=images,
        ))

    async def _on_document(self, update: Update, context: object) -> None:
        sender_id = self._accept(update)
        if sender_id is None:
            return
        doc = update.effective_message.document
        if doc is None:
            return
        file = await doc.get_file()
        attachments: list[Path] = []
        if self._inbox is not None:
            self._inbox.mkdir(parents=True, exist_ok=True)
            # Path(...).name strips any directory part: doc.file_name is
            # remote-controlled, and the allow-list admits everyone when
            # no allowed_users is configured, which is the default.
            safe_name = Path(doc.file_name).name if doc.file_name else ""
            # Path("..").name is "..", not "", so the fallback below did not
            # fire and the destination resolved to the inbox's own parent.
            # The download then wrote file content at a directory path.
            if safe_name in ("..", "."):
                safe_name = ""
            dest = self._inbox / (safe_name or f"doc_{file.file_unique_id}")
            await file.download_to_drive(str(dest))
            logger.info("saved document to %s", dest)
            attachments.append(dest)
        self._queue.put(InboundMessage(
            sender_id=sender_id,
            text=update.effective_message.caption or f"[document: {doc.file_name or 'unknown'}]",
            channel_id=self.channel_id,
            timestamp=datetime.now(timezone.utc),
            group_id=_group_id(update),
            attachments=attachments,
        ))
