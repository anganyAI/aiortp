"""VideoRTPSession — RTP session for video with codec-aware depacketization.

Supports H.264 (RFC 6184), VP8 (RFC 7741), and VP9 (RFC 9628)
with RTCP feedback.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .base_session import BaseRTPSession
from .h264 import H264Depacketizer, H264Packetizer, is_keyframe_nal
from .jitterbuffer import JitterBuffer
from .packet import (
    RTCP_PSFB_PLI,
    RTCP_RTPFB_NACK,
    RtcpByePacket,
    RtcpPacket,
    RtcpPsfbPacket,
    RtcpRtpfbPacket,
    RtpPacket,
)
from .port_allocator import PortAllocator
from .stats import NackGenerator, StreamStatistics
from .vp8 import VP8Depacketizer, VP8Packetizer
from .vp9 import VP9Depacketizer, VP9Packetizer

logger = logging.getLogger(__name__)

# Default video jitter buffer capacity (must be power of 2)
_VIDEO_JITTER_CAPACITY = 128

# Limit pending timestamps to prevent unbounded memory growth
_MAX_PENDING_TIMESTAMPS = 32


@dataclass
class _VideoCodecHandler:
    """Bundle of codec-specific components for a video codec."""

    depacketizer: Any  # has .feed(payload, marker) and .reset()
    packetizer: Any  # has .packetize(frame, ...)
    # True for H.264 (feed returns list[bytes], keyframe checked per NAL).
    # False for VP8/VP9 (feed returns list[tuple[bytes, bool]]).
    nal_mode: bool


def _make_h264() -> _VideoCodecHandler:
    return _VideoCodecHandler(H264Depacketizer(), H264Packetizer(), nal_mode=True)


def _make_vp8() -> _VideoCodecHandler:
    return _VideoCodecHandler(VP8Depacketizer(), VP8Packetizer(), nal_mode=False)


def _make_vp9() -> _VideoCodecHandler:
    return _VideoCodecHandler(VP9Depacketizer(), VP9Packetizer(), nal_mode=False)


_CODEC_FACTORIES: dict[str, Callable[[], _VideoCodecHandler]] = {
    "h264": _make_h264,
    "vp8": _make_vp8,
    "vp9": _make_vp9,
}

SUPPORTED_VIDEO_CODECS = frozenset(_CODEC_FACTORIES)


class VideoRTPSession(BaseRTPSession):
    """RTP session for video with codec-aware depacketization and RTCP feedback.

    Inbound: RTP packets -> jitter buffer -> depacketizer -> frames -> callback
    Outbound: frames -> packetizer -> RTP packets -> network
    """

    def __init__(
        self,
        payload_type: int = 96,
        ssrc: int | None = None,
        clock_rate: int = 90000,
        cname: str = "aiortp",
        rtcp_interval: float = 5.0,
        jitter_capacity: int = _VIDEO_JITTER_CAPACITY,
        codec: str = "h264",
        port_allocator: PortAllocator | None = None,
        fps: int = 30,
    ) -> None:
        factory = _CODEC_FACTORIES.get(codec)
        if factory is None:
            raise ValueError(
                f"Unsupported video codec {codec!r}, "
                f"must be one of {sorted(SUPPORTED_VIDEO_CODECS)}"
            )
        super().__init__(
            payload_type=payload_type,
            ssrc=ssrc,
            clock_rate=clock_rate,
            cname=cname,
            rtcp_interval=rtcp_interval,
            port_allocator=port_allocator,
        )
        self._codec = codec
        self._fps = fps
        self._timestamp_increment = clock_rate // fps
        self._handler = factory()
        self._jitter_buffer = JitterBuffer(
            capacity=jitter_capacity, prefetch=0, is_video=True
        )
        self._nack_gen = NackGenerator()

        # Per-timestamp payload storage: (sequence_number, payload) tuples.
        # Sorted by sequence number before depacketization to ensure
        # correct fragment ordering regardless of arrival order.
        self._pending_payloads: dict[int, list[tuple[int, bytes]]] = {}

        # Remote SSRC (learned from first inbound packet)
        self._remote_ssrc: int | None = None

        # Callbacks
        self.on_frame: Callable[[bytes, int, bool], None] | None = None
        """Called with (frame_data, timestamp, is_keyframe) for each frame."""

        self.on_keyframe_needed: Callable[[], None] | None = None
        """Called when a keyframe should be generated (local encoder feedback)."""

        # State
        self._rtp_packet_count = 0
        self._awaiting_keyframe = False

    @classmethod
    async def create(
        cls,
        local_addr: tuple[str, int],
        remote_addr: tuple[str, int],
        payload_type: int = 96,
        ssrc: int | None = None,
        clock_rate: int = 90000,
        cname: str = "aiortp",
        rtcp_interval: float = 5.0,
        jitter_capacity: int = _VIDEO_JITTER_CAPACITY,
        codec: str = "h264",
        port_allocator: PortAllocator | None = None,
        fps: int = 30,
    ) -> VideoRTPSession:
        """Async factory to create and bind a video RTP session."""
        session = cls(
            payload_type=payload_type,
            ssrc=ssrc,
            clock_rate=clock_rate,
            cname=cname,
            rtcp_interval=rtcp_interval,
            jitter_capacity=jitter_capacity,
            codec=codec,
            port_allocator=port_allocator,
            fps=fps,
        )
        await session._bind_transports(local_addr, remote_addr)
        if session._sender is not None:
            session._sender.timestamp_increment = session._timestamp_increment
        return session

    # ── Inbound ──────────────────────────────────────────────

    def _handle_rtp(self, data: bytes) -> None:
        """Handle incoming RTP packet."""
        self._rtp_packet_count += 1
        if self._rtp_packet_count == 1:
            logger.info("First UDP datagram on video port: %d bytes", len(data))

        try:
            packet = RtpPacket.parse(data)
        except ValueError:
            logger.warning("Failed to parse video RTP packet (len=%d)", len(data))
            return

        if packet.payload_type != self._payload_type:
            if self._rtp_packet_count <= 5:
                logger.info(
                    "Ignoring video RTP pt=%d (expected %d), seq=%d",
                    packet.payload_type, self._payload_type,
                    packet.sequence_number,
                )
            return

        # Learn remote SSRC
        if self._remote_ssrc is None:
            self._remote_ssrc = packet.ssrc
            logger.info(
                "First video RTP packet: ssrc=%d, pt=%d, seq=%d, len=%d",
                packet.ssrc, packet.payload_type,
                packet.sequence_number, len(packet.payload),
            )

        # Stream statistics
        if self._stream_stats is None:
            self._stream_stats = StreamStatistics(clockrate=self._clock_rate)
        self._stream_stats.add(packet)

        # NACK tracking
        if self._nack_gen.add(packet):
            self._send_nack()

        # Store (seq, payload) for ordered depacketization
        ts = packet.timestamp
        if ts not in self._pending_payloads:
            self._pending_payloads[ts] = []
        self._pending_payloads[ts].append(
            (packet.sequence_number, packet.payload)
        )
        self._evict_old_payloads()

        # Jitter buffer handles reordering and frame boundary detection
        pli_flag, frame = self._jitter_buffer.add(packet)

        if pli_flag:
            self._send_pli()
            self._handler.depacketizer.reset()
            self._awaiting_keyframe = True
            # Keep payloads for the frame being delivered (if any);
            # clear stale timestamps that were dropped by the jitter buffer.
            if frame is not None:
                keep = frame.timestamp
                self._pending_payloads = {
                    t: v for t, v in self._pending_payloads.items()
                    if t == keep
                }
            else:
                self._pending_payloads.clear()

        if frame is not None:
            self._deliver_frame(frame.timestamp)

    def _evict_old_payloads(self) -> None:
        """Evict oldest pending timestamps when the dict grows too large."""
        if len(self._pending_payloads) <= _MAX_PENDING_TIMESTAMPS:
            return
        sorted_ts = sorted(self._pending_payloads)
        for ts in sorted_ts[:-_MAX_PENDING_TIMESTAMPS]:
            del self._pending_payloads[ts]

    def _deliver_frame(self, timestamp: int) -> None:
        """Depacketize stored payloads for a completed frame and deliver."""
        entries = self._pending_payloads.pop(timestamp, [])
        if self.on_frame is None or not entries:
            return
        # Sort by sequence number for correct depacketization order
        entries.sort(key=lambda e: e[0])
        payloads = [payload for _, payload in entries]

        if self._handler.nal_mode:
            self._deliver_nal_mode(payloads, timestamp)
        else:
            self._deliver_frame_mode(payloads, timestamp)

    def _deliver_nal_mode(self, payloads: list[bytes], timestamp: int) -> None:
        """Deliver H.264 NAL units (feed returns list[bytes])."""
        depkt = self._handler.depacketizer
        for i, payload in enumerate(payloads):
            is_last = i == len(payloads) - 1
            nals = depkt.feed(payload, marker=is_last)
            for nal in nals:
                is_key = is_keyframe_nal(nal)
                if self._awaiting_keyframe:
                    if is_key:
                        self._awaiting_keyframe = False
                    else:
                        continue
                self.on_frame(nal, timestamp, is_key)  # type: ignore[misc]

    def _deliver_frame_mode(self, payloads: list[bytes], timestamp: int) -> None:
        """Deliver VP8/VP9 frames (feed returns list[tuple[bytes, bool]])."""
        depkt = self._handler.depacketizer
        for i, payload in enumerate(payloads):
            is_last = i == len(payloads) - 1
            frames = depkt.feed(payload, marker=is_last)
            for frame_data, is_keyframe in frames:
                if self._awaiting_keyframe:
                    if is_keyframe:
                        self._awaiting_keyframe = False
                    else:
                        continue
                self.on_frame(frame_data, timestamp, is_keyframe)  # type: ignore[misc]

    def _handle_rtcp(self, data: bytes) -> None:
        """Handle incoming RTCP packet."""
        try:
            packets = RtcpPacket.parse(data)
        except ValueError:
            logger.warning("Failed to parse video RTCP packet")
            return

        for packet in packets:
            if isinstance(packet, RtcpPsfbPacket) and packet.fmt == RTCP_PSFB_PLI:
                if self.on_keyframe_needed is not None:
                    self.on_keyframe_needed()
            elif isinstance(packet, RtcpByePacket):
                logger.info("Video: received RTCP BYE from %s", packet.sources)

    # ── Outbound ─────────────────────────────────────────────

    def send_frame(
        self, nal_units: list[bytes], timestamp: int, keyframe: bool = False
    ) -> None:
        """Packetize and send video data as RTP packets.

        Args:
            nal_units: For H.264: list of NAL units. For VP8/VP9: list of
                complete frame bitstreams (typically one per call).
            timestamp: RTP timestamp (90kHz clock).
            keyframe: Whether this is a keyframe.
        """
        if self._sender is None or self._closed:
            return

        packetizer = self._handler.packetizer
        all_packets: list[tuple[bytes, bool]] = []

        if self._handler.nal_mode:
            for nal in nal_units:
                all_packets.extend(packetizer.packetize(nal))
        else:
            for frame in nal_units:
                all_packets.extend(packetizer.packetize(frame, keyframe=keyframe))

        if not all_packets:
            return

        # Marker=1 only on the last packet of the access unit
        for i, (payload, _) in enumerate(all_packets):
            is_last = i == len(all_packets) - 1
            self._sender.send_frame(
                payload, timestamp,
                marker=1 if is_last else 0,
                addr=self._remote_addr,
            )

    def send_frame_auto(
        self, nal_units: list[bytes], keyframe: bool = False,
    ) -> int:
        """Packetize and send video with auto-incrementing timestamp.

        Returns the RTP timestamp used for this frame.
        """
        if self._sender is None or self._closed:
            return 0
        ts = self._sender.current_timestamp
        self.send_frame(nal_units, ts, keyframe)
        self._sender.advance_timestamp()
        return ts

    # ── RTCP feedback ────────────────────────────────────────

    def _send_pli(self) -> None:
        """Send RTCP PLI to request a keyframe."""
        if self._rtcp_transport is None or self._remote_ssrc is None:
            return
        pli = RtcpPsfbPacket(
            fmt=RTCP_PSFB_PLI,
            ssrc=self._ssrc,
            media_ssrc=self._remote_ssrc,
        )
        self._rtcp_transport.send(bytes(pli), self._remote_rtcp_addr)
        logger.debug("Sent PLI to remote SSRC %d", self._remote_ssrc)

    def request_keyframe(self) -> None:
        """Explicitly request a keyframe from the remote sender via PLI."""
        self._send_pli()

    def _send_nack(self) -> None:
        """Send RTCP NACK for missing packets."""
        if (
            self._rtcp_transport is None
            or self._remote_ssrc is None
            or not self._nack_gen.missing
        ):
            return
        nack = RtcpRtpfbPacket(
            fmt=RTCP_RTPFB_NACK,
            ssrc=self._ssrc,
            media_ssrc=self._remote_ssrc,
            lost=sorted(self._nack_gen.missing),
        )
        self._rtcp_transport.send(bytes(nack), self._remote_rtcp_addr)

    # ── Lifecycle ────────────────────────────────────────────

    def _on_closing(self) -> None:
        """Log session statistics before closing."""
        received = 0
        if self._stream_stats is not None:
            received = self._stream_stats.packets_received
        logger.info(
            "Video RTP session closing: udp_packets=%d, rtp_matched=%d",
            self._rtp_packet_count, received,
        )
