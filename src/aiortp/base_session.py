"""BaseRTPSession — shared RTP/RTCP session lifecycle.

Extracts common transport binding, RTCP loop, and lifecycle
management from RTPSession and VideoRTPSession.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from . import clock
from .packet import (
    RtcpByePacket,
    RtcpSdesPacket,
    RtcpSenderInfo,
    RtcpSourceInfo,
    RtcpSrPacket,
)
from .sender import RtpSender
from .stats import StreamStatistics
from .transport import RtpTransport
from .utils import random32

logger = logging.getLogger(__name__)


class BaseRTPSession:
    """Base class for RTP sessions with shared transport and RTCP lifecycle.

    Subclasses implement ``_handle_rtp`` and ``_handle_rtcp`` for
    media-specific processing.
    """

    def __init__(
        self,
        payload_type: int,
        ssrc: int | None = None,
        clock_rate: int = 8000,
        cname: str = "aiortp",
        rtcp_interval: float = 5.0,
    ) -> None:
        self._payload_type = payload_type
        self._ssrc = ssrc if ssrc is not None else random32()
        self._clock_rate = clock_rate
        self._cname = cname
        self._rtcp_interval = rtcp_interval

        # Transport
        self._rtp_transport: RtpTransport | None = None
        self._rtcp_transport: RtpTransport | None = None
        self._remote_addr: tuple[str, int] | None = None
        self._remote_rtcp_addr: tuple[str, int] | None = None

        # Sender
        self._sender: RtpSender | None = None

        # Stats (initialized lazily by subclasses on first inbound packet)
        self._stream_stats: StreamStatistics | None = None

        # RTCP
        self._rtcp_task: asyncio.Task[None] | None = None

        # State
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None

    @staticmethod
    def _compute_rtcp_addr(addr: tuple[str, int]) -> tuple[str, int]:
        """Derive RTCP address from RTP address, handling port 0."""
        if addr[1] == 0:
            return addr
        return (addr[0], addr[1] + 1)

    async def _bind_transports(
        self,
        local_addr: tuple[str, int],
        remote_addr: tuple[str, int],
    ) -> None:
        """Bind RTP and RTCP UDP transports, create sender, start RTCP."""
        self._loop = asyncio.get_running_loop()
        self._remote_addr = remote_addr
        self._remote_rtcp_addr = self._compute_rtcp_addr(remote_addr)

        # RTP transport
        rtp_transport_obj = RtpTransport(
            on_rtp=self._handle_rtp,
            on_rtcp=self._handle_rtcp,
        )
        await self._loop.create_datagram_endpoint(
            lambda: rtp_transport_obj,
            local_addr=local_addr,
        )
        self._rtp_transport = rtp_transport_obj

        # RTCP transport — port adjacent to RTP, or OS-assigned
        rtp_bound = rtp_transport_obj._transport.get_extra_info("sockname")  # type: ignore[union-attr]
        if local_addr[1] == 0:
            rtcp_local = (local_addr[0], 0)
        else:
            rtcp_local = (local_addr[0], rtp_bound[1] + 1)

        rtcp_transport_obj = RtpTransport(
            on_rtp=self._handle_rtp,
            on_rtcp=self._handle_rtcp,
        )
        await self._loop.create_datagram_endpoint(
            lambda: rtcp_transport_obj,
            local_addr=rtcp_local,
        )
        self._rtcp_transport = rtcp_transport_obj

        # Sender
        self._sender = RtpSender(
            transport=self._rtp_transport,
            payload_type=self._payload_type,
            ssrc=self._ssrc,
            clock_rate=self._clock_rate,
        )

        # Start RTCP loop
        self._rtcp_task = asyncio.create_task(self._run_rtcp())

    def update_remote(self, addr: tuple[str, int]) -> None:
        """Update remote address (e.g., for re-INVITE)."""
        self._remote_addr = addr
        self._remote_rtcp_addr = self._compute_rtcp_addr(addr)

    # ── RTCP ──────────────────────────────────────────────────

    async def _run_rtcp(self) -> None:
        """Periodic RTCP sender loop."""
        try:
            while not self._closed:
                interval = self._rtcp_interval * (0.5 + random.random())
                await asyncio.sleep(interval)
                if self._closed:
                    break
                self._send_sr()
        except asyncio.CancelledError:
            pass

    def _send_sr(self) -> None:
        """Send RTCP Sender Report + SDES."""
        if self._rtcp_transport is None or self._sender is None:
            return
        sr = RtcpSrPacket(
            ssrc=self._ssrc,
            sender_info=RtcpSenderInfo(
                ntp_timestamp=clock.current_ntp_time(),
                rtp_timestamp=0,
                packet_count=self._sender.packets_sent,
                octet_count=self._sender.octets_sent,
            ),
        )
        sdes = RtcpSdesPacket(
            chunks=[
                RtcpSourceInfo(
                    ssrc=self._ssrc,
                    items=[(1, self._cname.encode("utf-8"))],
                )
            ]
        )
        self._rtcp_transport.send(
            bytes(sr) + bytes(sdes), self._remote_rtcp_addr
        )

    def _send_bye(self) -> None:
        """Send RTCP BYE."""
        if self._rtcp_transport is None:
            return
        bye = RtcpByePacket(sources=[self._ssrc])
        self._rtcp_transport.send(bytes(bye), self._remote_rtcp_addr)

    # ── Lifecycle ─────────────────────────────────────────────

    @property
    def stats(self) -> dict[str, Any]:
        """Return session statistics."""
        result: dict[str, Any] = {
            "ssrc": self._ssrc,
            "packets_sent": self._sender.packets_sent if self._sender else 0,
            "octets_sent": self._sender.octets_sent if self._sender else 0,
        }
        if self._stream_stats is not None:
            result["packets_received"] = self._stream_stats.packets_received
            result["packets_lost"] = self._stream_stats.packets_lost
            result["jitter"] = self._stream_stats.jitter
        return result

    def _on_closing(self) -> None:
        """Hook for subclass-specific close behavior."""

    async def close(self) -> None:
        """Close the session, sending BYE and releasing resources."""
        if self._closed:
            return
        self._closed = True

        self._on_closing()

        if self._rtcp_task is not None:
            self._rtcp_task.cancel()
            try:
                await self._rtcp_task
            except asyncio.CancelledError:
                pass

        self._send_bye()
        await asyncio.sleep(0)

        if self._rtp_transport is not None:
            self._rtp_transport.close()
        if self._rtcp_transport is not None:
            self._rtcp_transport.close()

    # ── Abstract ──────────────────────────────────────────────

    def _handle_rtp(self, data: bytes) -> None:
        raise NotImplementedError

    def _handle_rtcp(self, data: bytes) -> None:
        raise NotImplementedError
