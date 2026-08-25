from typing import Protocol, runtime_checkable

from faffmonkey.types import InboundMessage, OutboundMessage


@runtime_checkable
class Channel(Protocol):
    def start(self) -> None:
        """Bring the channel up and RETURN.

        Must not block. A channel that needs its own event loop runs it on
        an internal thread and returns once the channel is usable.
        """
        ...

    def stop(self) -> None: ...

    def receive(self) -> InboundMessage | None:
        """Return the next message, or None if there is nothing right now.

        None means "nothing yet", never "we are finished". Use is_closed()
        to signal that the channel is done.
        """
        ...

    def send(self, message: OutboundMessage) -> None: ...
    def is_allowed(self, sender_id: str) -> bool: ...
    def poll(self) -> InboundMessage | None: ...

    def is_closed(self) -> bool:
        """True once the channel is finished and will produce no more input."""
        ...
