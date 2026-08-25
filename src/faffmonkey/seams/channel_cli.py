import sys
from datetime import datetime, timezone

from faffmonkey.types import InboundMessage, OutboundMessage


class CLIChannel:
    channel_id: str = "cli"

    def __init__(self) -> None:
        self._closed = False

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def is_closed(self) -> bool:
        return self._closed

    def is_allowed(self, sender_id: str) -> bool:
        return True

    def receive(self) -> InboundMessage | None:
        try:
            text = input("you> ")
        except (EOFError, KeyboardInterrupt):
            self._closed = True
            return None
        if not text.strip():
            # Nothing to do, not the end of the session: returning None here
            # reads as EOF and quits the chat.
            return None
        return InboundMessage(
            sender_id="cli-user",
            text=text,
            channel_id=self.channel_id,
            timestamp=datetime.now(timezone.utc),
        )

    def poll(self) -> InboundMessage | None:
        return None

    def send(self, message: OutboundMessage) -> None:
        sys.stdout.write(f"agent> {message.text}\n")
        for path in message.attachments:
            sys.stdout.write(f"  [attachment: {path}]\n")
        sys.stdout.flush()
