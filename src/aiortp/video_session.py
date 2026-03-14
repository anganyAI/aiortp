"""VideoRTPSession — RTP session for video with codec-aware depacketization.

Supports H.264 (RFC 6184) and VP9 (RFC 9628) with RTCP feedback.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

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
from .stats import NackGenerator, StreamStatistics
from .vp9 import VP9Depacketizer, VP9Packetizer

logger = logging.getLogger(__name__)

# Supported video codecs
SUPPORTED_VIDEO_CODECS = frozenset({"h264", "vp9"})

# Default video jitter buffer capacity (must be power of 2)
_VIDEO_JITTER_CAPACITY = 128

# Limit pending timestamps to prevent unbounded memory growth
_MAX_PENDING_TIMESTAMPS = 32


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
    ) -> None:
        if codec not in SUPPORTED_VIDEO_CODECS:
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
        )
        self._codec = codec
        self._jitter_buffer = JitterBuffer(
            capacity=jitter_capacity, prefetch=0, is_video=True
        )
        self._nack_gen = NackGenerator()

        # Codec-specific depacketizer/packetizer
        if codec == "vp9":
            self._vp9_depacketizer = VP9Depacketizer()
            self._vp9_packetizer = VP9Packetizer()
            self._h264_depacketizer: H264Depacketizer | None = None
            self._h264_packetizer: H264Packetizer | None = None
        else:
            self._h264_depacketizer = H264Depacketizer()
            self._h264_packetizer = H264Packetizer()
            self._vp9_depacketizer: VP9Depacketizer | None = None
            self._vp9_packetizer: VP9Packetizer | None = None

        # Per-timestamp payload storage: (sequence_number, payload) tuples.
        # Sorted by sequence number before depacketization to ensure
        # correct FU-A fragment ordering regardless of arrival order.
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
        )
        await session._bind_transports(local_addr, remote_addr)
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
            self._reset_depacketizer()
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

    def _reset_depacketizer(self) -> None:
        """Reset the active depacketizer."""
        if self._h264_depacketizer is not None:
            self._h264_depacketizer.reset()
        if self._vp9_depacketizer is not None:
            self._vp9_depacketizer.reset()

    def _deliver_frame(self, timestamp: int) -> None:
        """Depacketize stored payloads for a completed frame and deliver."""
        entries = self._pending_payloads.pop(timestamp, [])
        if self.on_frame is None or not entries:
            return
        # Sort by sequence number for correct depacketization order
        entries.sort(key=lambda e: e[0])
        payloads = [payload for _, payload in entries]

        if self._codec == "vp9":
            self._deliver_vp9(payloads, timestamp)
        else:
            self._deliver_h264(payloads, timestamp)

    def _deliver_h264(self, payloads: list[bytes], timestamp: int) -> None:
        """Depacketize H.264 NAL units and deliver."""
        assert self._h264_depacketizer is not None
        for i, payload in enumerate(payloads):
            is_last = i == len(payloads) - 1
            nals = self._h264_depacketizer.feed(payload, marker=is_last)
            for nal in nals:
                is_key = is_keyframe_nal(nal)
                if self._awaiting_keyframe:
                    if is_key:
                        self._awaiting_keyframe = False
                    else:
                        continue
                self.on_frame(nal, timestamp, is_key)  # type: ignore[misc]

    def _deliver_vp9(self, payloads: list[bytes], timestamp: int) -> None:
        """Depacketize VP9 frame data and deliver."""
        assert self._vp9_depacketizer is not None
        for i, payload in enumerate(payloads):
            is_last = i == len(payloads) - 1
            frames = self._vp9_depacketizer.feed(payload, marker=is_last)
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
            nal_units: For H.264: list of NAL units. For VP9: list of
                complete frame bitstreams (typically one per call).
            timestamp: RTP timestamp (90kHz clock).
            keyframe: Whether this is a keyframe.
        """
        if self._sender is None or self._closed:
            return

        all_packets: list[tuple[bytes, bool]] = []

        if self._codec == "vp9" and self._vp9_packetizer is not None:
            for frame in nal_units:
                all_packets.extend(
                    self._vp9_packetizer.packetize(frame, keyframe=keyframe)
                )
        elif self._h264_packetizer is not None:
            for nal in nal_units:
                all_packets.extend(self._h264_packetizer.packetize(nal))

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
