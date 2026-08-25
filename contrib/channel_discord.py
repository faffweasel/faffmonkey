"""Discord channel via discord.py. Contrib extension."""

import asyncio
import io
import json
import logging
import os
import queue
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

import discord

from faffmonkey.types import InboundMessage, OutboundMessage

logger = logging.getLogger(__name__)

DISCORD_MAX_LENGTH = 2000


def _split_message(text: str, limit: int = DISCORD_MAX_LENGTH) -> list[str]:
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


class DiscordChannel:
    channel_id: str = "discord"

    def __init__(
        self,
        allowed_users: list[str] | None = None,
        workspace: Path | None = None,
        group_policy: str = "mention",
    ) -> None:
        token = os.environ.get("DISCORD_BOT_TOKEN", "")
        if not token:
            raise RuntimeError("DISCORD_BOT_TOKEN not set")
        self._token = token
        self._allowed_users: set[str] = set(allowed_users or [])
        self._group_policy = group_policy
        self._queue: queue.Queue[InboundMessage] = queue.Queue()
        self._client: discord.Client | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reply_channel = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._closed = False
        self._inbox = Path(workspace / "shared" / "inbox") if workspace else None
        # state/ is the workspace's sibling, the same derivation the skill
        # scripts use. The reply channel belongs there and not in workspace/,
        # which is the agent's world.
        self._state_path = (
            workspace.parent / "state" / "channel-discord.json" if workspace else None
        )
        self._reply_channel_id: int | None = self._load_channel_id()

    def _load_channel_id(self) -> int | None:
        """The channel this bot last replied in, from a previous process.

        Without this, a cron job firing after a container restart and before
        the operator has said anything found no reply channel, returned from
        send() without raising, and the scheduler recorded the run as
        delivered. The briefing was in the session and had gone nowhere.
        """
        if self._state_path is None or not self._state_path.is_file():
            return None
        try:
            data = json.loads(self._state_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("cannot read %s: %s", self._state_path, e)
            return None
        channel_id = data.get("reply_channel_id") if isinstance(data, dict) else None
        return channel_id if isinstance(channel_id, int) else None

    async def _restore_reply_channel(self) -> None:
        """Resolve the persisted reply target after a restart.

        get_channel only reads the cache, and a DM channel is not in it
        until a message arrives; fetch_channel asks the API.
        """
        if self._client is None or self._reply_channel is not None:
            return
        if self._reply_channel_id is None:
            return
        restored = self._client.get_channel(self._reply_channel_id)
        if restored is None:
            try:
                restored = await self._client.fetch_channel(self._reply_channel_id)
            except Exception as e:
                logger.warning(
                    "discord reply channel %s is no longer reachable: %s",
                    self._reply_channel_id, e,
                )
                return
        self._reply_channel = restored

    def _remember_channel(self, channel: object) -> None:
        self._reply_channel = channel
        channel_id = getattr(channel, "id", None)
        if not isinstance(channel_id, int) or channel_id == self._reply_channel_id:
            return
        self._reply_channel_id = channel_id
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"reply_channel_id": channel_id}) + "\n")
            os.replace(tmp, self._state_path)
        except OSError as e:
            logger.warning("cannot persist discord reply channel: %s", e)

    async def _save_attachment(self, attachment: object, kind: str) -> Path | None:
        """Land an inbound attachment in the inbox and return its path.

        The path, not the bytes, is what reaches the session: history stays
        small and the file is still addressable by the agent's file tools.
        """
        if self._inbox is None:
            return None
        try:
            self._inbox.mkdir(parents=True, exist_ok=True)
            # Path(...).name strips any directory part. The id prefix alone
            # does not stop traversal: "discord_1_../../x" still escapes.
            safe_name = Path(attachment.filename).name
            dest = self._inbox / f"discord_{attachment.id}_{safe_name}"
            await attachment.save(dest)
        except Exception as e:
            logger.warning("failed to save discord %s attachment: %s", kind, e)
            return None
        logger.info("saved %s to %s", kind, dest)
        return dest

    def is_allowed(self, sender_id: str) -> bool:
        if not self._allowed_users:
            return False
        return sender_id in self._allowed_users

    def _should_respond(self, is_dm: bool, is_mentioned: bool, sender_id: str) -> bool:
        if not self.is_allowed(sender_id):
            return False
        if is_dm:
            return True
        if self._group_policy == "open":
            return True
        return self._group_policy == "mention" and is_mentioned

    def start(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        self._client = discord.Client(intents=intents)

        @self._client.event
        async def on_ready() -> None:
            self._loop = asyncio.get_running_loop()
            # Restore the last reply target before anything can send, so a
            # cron job firing before the operator speaks has somewhere to go.
            await self._restore_reply_channel()
            self._ready.set()
            logger.info("discord bot ready as %s", self._client.user)

        @self._client.event
        async def on_message(message: discord.Message) -> None:
            if self._client is None or self._client.user is None:
                return
            if message.author.id == self._client.user.id:
                return

            sender_id = str(message.author.id)
            is_dm = isinstance(message.channel, discord.DMChannel)
            is_mentioned = self._client.user in message.mentions

            if not self._should_respond(is_dm, is_mentioned, sender_id):
                return

            text = message.content
            if is_mentioned:
                bot_id = str(self._client.user.id)
                text = re.sub(rf"<@!?{bot_id}>", "", text).strip()

            audio: bytes | None = None
            audio_mime: str | None = None
            images: list[Path] = []
            attachments: list[Path] = []
            for attachment in message.attachments:
                content_type = (attachment.content_type or "").split(";")[0].strip()
                if audio is None and content_type.startswith("audio/"):
                    try:
                        audio = await attachment.read()
                        audio_mime = content_type
                    except Exception as e:
                        logger.warning("failed to read discord audio attachment: %s", e)
                    continue
                if content_type.startswith("image/"):
                    dest = await self._save_attachment(attachment, "image")
                    if dest is not None:
                        images.append(dest)
                    continue
                # Any other attachment type is saved as a file so its content
                # is not lost.
                dest = await self._save_attachment(attachment, "file")
                if dest is not None:
                    attachments.append(dest)

            # An attachment-only message leaves text empty, which produced
            # an LLM request message with no content field at all. Strict
            # providers reject that, and because it is persisted it was
            # replayed on every later turn, so one wordless photo broke the
            # conversation permanently.
            if not text.strip():
                if images and audio is not None:
                    text = "(sent an image and a voice message)"
                elif images:
                    text = "(sent an image)" if len(images) == 1 else "(sent images)"
                elif audio is not None:
                    text = "(sent a voice message)"
                elif attachments:
                    names = ", ".join(p.name for p in attachments)
                    text = f"(sent a file: {names})"
                else:
                    text = "(sent an empty message)"

            # Only a DM is remembered as the announcement target; a heartbeat
            # composed from the owner's conversation must not land in a guild
            # room.
            if is_dm:
                self._remember_channel(message.channel)
            self._queue.put(InboundMessage(
                sender_id=sender_id,
                text=text,
                channel_id=self.channel_id,
                timestamp=datetime.now(timezone.utc),
                audio=audio,
                audio_mime=audio_mime,
                images=images,
                attachments=attachments,
                # A guild room is read by everyone in it, so it gets its
                # own session instead of the shared direct conversation.
                group_id=None if is_dm else str(message.channel.id),
            ))

        self._thread = threading.Thread(
            target=self._run_forever, daemon=True, name="discord-client",
        )
        self._thread.start()
        # Wait for on_ready before returning, so an immediate send() has an
        # event loop to submit to.
        if not self._ready.wait(timeout=30):
            logger.warning("discord did not become ready within 30s")

    def _run_forever(self) -> None:
        try:
            self._client.run(self._token, log_handler=None)
        except Exception:
            logger.exception("discord client stopped")
        finally:
            self._closed = True
            self._ready.set()

    def stop(self) -> None:
        self._closed = True
        if self._client is not None and self._loop is not None:
            future = asyncio.run_coroutine_threadsafe(
                self._client.close(), self._loop,
            )
            try:
                future.result(timeout=10)
            except Exception:
                logger.warning("discord client close timed out")
        if self._thread is not None:
            self._thread.join(timeout=10)

    def is_closed(self) -> bool:
        return self._closed

    def receive(self) -> InboundMessage | None:
        try:
            return self._queue.get(timeout=1.0)
        except queue.Empty:
            return None

    def _target(self, group_id: str | None) -> object:
        """The room a message goes to: the one it answers, else the owner's DM."""
        if group_id is not None:
            channel_id = int(group_id)
            room = self._client.get_channel(channel_id)
            if room is None:
                room = asyncio.run_coroutine_threadsafe(
                    self._client.fetch_channel(channel_id), self._loop,
                ).result(timeout=30)
            return room
        if self._reply_channel is None:
            # Returning quietly here let the scheduler record a delivery
            # that never happened as a success.
            raise RuntimeError(
                "discord has no reply target yet: nobody has messaged the "
                "bot since it started and the saved channel could not be "
                "restored"
            )
        return self._reply_channel

    def send(self, message: OutboundMessage) -> None:
        if self._client is None or self._loop is None:
            return
        target = self._target(message.group_id)
        for chunk in _split_message(message.text):
            future = asyncio.run_coroutine_threadsafe(
                target.send(chunk), self._loop,
            )
            try:
                future.result(timeout=30)
            except Exception:
                logger.warning("failed to send discord message chunk")
                return
        if message.audio is not None:
            ext = "ogg" if message.audio_mime == "audio/ogg" else "wav"
            voice_file = discord.File(
                io.BytesIO(message.audio), filename=f"voice-reply.{ext}",
            )
            future = asyncio.run_coroutine_threadsafe(
                target.send(file=voice_file), self._loop,
            )
            try:
                future.result(timeout=30)
            except Exception:
                logger.warning("failed to send discord voice reply")
        for path in message.attachments:
            future = asyncio.run_coroutine_threadsafe(
                target.send(file=discord.File(str(path))),
                self._loop,
            )
            try:
                future.result(timeout=30)
            except Exception:
                logger.warning("failed to send discord attachment: %s", path)

    def poll(self) -> InboundMessage | None:
        """Non-blocking variant of receive, so the user can interrupt an
        active goal.
        """
        try:
            return self._queue.get_nowait()
        except queue.Empty:
            return None
