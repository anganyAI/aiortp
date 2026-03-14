"""BaseRTPSession — shared RTP/RTCP session lifecycle.

Extracts common transport binding, RTCP loop, and lifecycle
management from RTPSession and VideoRTPSession.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Callable
from typing import Any

from . import clock
from .packet import (
    RtcpByePacket,
    RtcpReceiverInfo,
    RtcpRrPacket,
    RtcpRtpfbPacket,
    RtcpSdesPacket,
    RtcpSenderInfo,
    RtcpSourceInfo,
    RtcpSrPacket,
)
from .port_allocator import PortAllocator
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
        port_allocator: PortAllocator | None = None,
    ) -> None:
        self._payload_type = payload_type
        self._ssrc = ssrc if ssrc is not None else random32()
        self._clock_rate = clock_rate
        self._cname = cname
        self._rtcp_interval = rtcp_interval
        self._port_allocator = port_allocator

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

        # Incoming SR tracking (for LSR/DLSR in receiver reports)
        self._last_sr_ntp: int | None = None
        self._last_sr_received_at: float | None = None

        # Remote SSRC (learned from first inbound packet by subclasses)
        self._remote_ssrc: int | None = None

        # Last received receiver report (for stats exposure)
        self._last_rr: RtcpReceiverInfo | None = None

        # Callback for incoming receiver reports
        self.on_receiver_report: Callable[[RtcpReceiverInfo], None] | None = None
        """Called when a Receiver Report block is received from the remote."""

        # Port tracking for allocator release on close
        self._allocated_rtp_port: int | None = None

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

        # Determine local addresses
        if self._port_allocator is not None:
            rtp_port, rtcp_port = await self._port_allocator.allocate()
            self._allocated_rtp_port = rtp_port
            rtp_local = (local_addr[0], rtp_port)
            rtcp_local = (local_addr[0], rtcp_port)
        else:
            rtp_local = local_addr
            rtcp_local = None  # computed after RTP bind

        # RTP transport
        rtp_transport_obj = RtpTransport(
            on_rtp=self._handle_rtp,
            on_rtcp=self._handle_rtcp,
        )
        await self._loop.create_datagram_endpoint(
            lambda: rtp_transport_obj,
            local_addr=rtp_local,
        )
        self._rtp_transport = rtp_transport_obj

        # RTCP transport — use allocator port, adjacent port, or OS-assigned
        if rtcp_local is None:
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
                interval = self._rtcp_interval * (0.5 + random.random())  # noqa: S311  # nosec B311 — RFC 3550 jitter
                await asyncio.sleep(interval)
                if self._closed:
                    break
                self._send_rtcp_report()
        except asyncio.CancelledError:
            pass

    def _send_rtcp_report(self) -> None:
        """Send SR (if sending) or RR (if receive-only), plus SDES."""
        if self._rtcp_transport is None:
            return

        sdes = RtcpSdesPacket(
            chunks=[
                RtcpSourceInfo(
                    ssrc=self._ssrc,
                    items=[(1, self._cname.encode("utf-8"))],
                )
            ]
        )

        # Build receiver report block if we have inbound stats
        rr_block = self._build_receiver_report()

        if self._sender is not None and self._sender.packets_sent > 0:
            sr = RtcpSrPacket(
                ssrc=self._ssrc,
                sender_info=RtcpSenderInfo(
                    ntp_timestamp=clock.current_ntp_time(),
                    rtp_timestamp=self._sender.last_rtp_timestamp,
                    packet_count=self._sender.packets_sent,
                    octet_count=self._sender.octets_sent,
                ),
                reports=[rr_block] if rr_block else [],
            )
            self._rtcp_transport.send(bytes(sr) + bytes(sdes), self._remote_rtcp_addr)
        elif rr_block is not None:
            rr = RtcpRrPacket(
                ssrc=self._ssrc,
                reports=[rr_block],
            )
            self._rtcp_transport.send(bytes(rr) + bytes(sdes), self._remote_rtcp_addr)

    def _build_receiver_report(self) -> RtcpReceiverInfo | None:
        """Build a receiver report block from stream statistics."""
        if self._stream_stats is None or self._remote_ssrc is None:
            return None

        stats = self._stream_stats

        # LSR: middle 32 bits of the last SR NTP timestamp
        lsr = 0
        dlsr = 0
        if self._last_sr_ntp is not None and self._last_sr_received_at is not None:
            lsr = (self._last_sr_ntp >> 16) & 0xFFFFFFFF
            # DLSR: delay since last SR in 1/65536 seconds
            delay = time.monotonic() - self._last_sr_received_at
            dlsr = int(delay * 65536) & 0xFFFFFFFF

        return RtcpReceiverInfo(
            ssrc=self._remote_ssrc,
            fraction_lost=stats.fraction_lost,
            packets_lost=stats.packets_lost,
            highest_sequence=stats.cycles + (stats.max_seq or 0),
            jitter=stats.jitter,
            lsr=lsr,
            dlsr=dlsr,
        )

    def _record_incoming_sr(self, ntp_timestamp: int) -> None:
        """Record an incoming SR for LSR/DLSR computation."""
        self._last_sr_ntp = ntp_timestamp
        self._last_sr_received_at = time.monotonic()

    def _handle_incoming_nack(self, packet: RtcpRtpfbPacket) -> None:
        """Retransmit packets requested by a NACK."""
        if self._sender is None or not packet.lost:
            return
        self._sender.retransmit(packet.lost, self._remote_addr)

    def _process_receiver_reports(self, reports: list[RtcpReceiverInfo]) -> None:
        """Process incoming RR blocks from SR or RR packets."""
        for report in reports:
            if report.ssrc == self._ssrc:
                self._last_rr = report
                if self.on_receiver_report is not None:
                    self.on_receiver_report(report)

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
        if self._last_rr is not None:
            result["remote_fraction_lost"] = self._last_rr.fraction_lost
            result["remote_packets_lost"] = self._last_rr.packets_lost
            result["remote_jitter"] = self._last_rr.jitter
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

        if self._port_allocator is not None and self._allocated_rtp_port is not None:
            await self._port_allocator.release(self._allocated_rtp_port)

    # ── Abstract ──────────────────────────────────────────────

    def _handle_rtp(self, data: bytes) -> None:
        raise NotImplementedError

    def _handle_rtcp(self, data: bytes) -> None:
        raise NotImplementedError
