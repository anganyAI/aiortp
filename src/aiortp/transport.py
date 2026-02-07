import asyncio
import logging
from collections.abc import Callable
from typing import Optional

from .packet import is_rtcp

logger = logging.getLogger(__name__)


class RtpTransport(asyncio.DatagramProtocol):
    def __init__(
        self,
        on_rtp: Callable[[bytes], None],
        on_rtcp: Callable[[bytes], None],
    ) -> None:
        self._on_rtp = on_rtp
        self._on_rtcp = on_rtcp
        self._transport: Optional[asyncio.DatagramTransport] = None
        self._remote_addr: Optional[tuple[str, int]] = None
        self._closed = asyncio.Event()

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self._transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if is_rtcp(data):
            self._on_rtcp(data)
        else:
            self._on_rtp(data)

    def error_received(self, exc: Exception) -> None:
        logger.warning("Transport error: %s", exc)

    def connection_lost(self, exc: Optional[Exception]) -> None:
        self._closed.set()

    def send(self, data: bytes, addr: Optional[tuple[str, int]] = None) -> None:
        if self._transport is None:
            return
        target = addr or self._remote_addr
        if target is not None:
            self._transport.sendto(data, target)

    def close(self) -> None:
        if self._transport is not None:
            self._transport.close()
