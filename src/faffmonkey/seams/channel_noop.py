from faffmonkey.types import InboundMessage, OutboundMessage


class NoopChannel:
    channel_id: str = "noop"

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def is_closed(self) -> bool:
        # Nothing will ever arrive, so a run loop over this channel should
        # terminate rather than spin.
        return True

    def is_allowed(self, sender_id: str) -> bool:
        return False

    def receive(self) -> InboundMessage | None:
        return None

    def send(self, message: OutboundMessage) -> None:
        pass

    def poll(self) -> InboundMessage | None:
        return None
