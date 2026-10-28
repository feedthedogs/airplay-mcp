from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ReceiverStatus:
    running: bool
    advertised_name: str
    detail: str = ""
    client: str | None = None  # e.g. "iPhone (iPhone15,2)"
    log_tail: list[str] = field(default_factory=list)


class Receiver(ABC):
    """A screen-mirroring endpoint that pushes video to a StreamDecoder port."""

    @abstractmethod
    def start(self) -> None:
        """Start advertising. Must be idempotent."""

    @abstractmethod
    def stop(self) -> None:
        """Stop advertising. The receiver may be started again afterwards."""

    @abstractmethod
    def status(self) -> ReceiverStatus: ...
