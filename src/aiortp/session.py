"""RTPSession — main orchestrator for sending/receiving RTP audio."""

import logging
from collections.abc import Callable
from typing import Any

from .base_session import BaseRTPSession
from .codecs import Codec, get_codec
from .dtmf import DtmfReceiver, DtmfSender
from .jitterbuffer import JitterBuffer
from .packet import (
    RTCP_RTPFB_NACK,
    RtcpByePacket,
    RtcpPacket,
    RtcpRrPacket,
    RtcpRtpfbPacket,
    RtcpSrPacket,
    RtpPacket,
)
from .plc import PcmConcealer
from .port_allocator import PortAllocator

logger = logging.getLogger(__name__)


class RTPSession(BaseRTPSession):
    def __init__(
        self,
        payload_type: int,
        codec: Codec | None = None,
        ssrc: int | None = None,
        clock_rate: int = 8000,
        dtmf_payload_type: int = 101,
        cname: str = "aiortp",
        rtcp_interval: float = 5.0,
        jitter_capacity: int = 16,
        jitter_prefetch: int = 4,
        skip_audio_gaps: bool = False,
        plc: bool = True,
        nack_retransmit: bool = False,
        symmetric_rtp: bool = False,
        port_allocator: PortAllocator | None = None,
    ) -> None:
        super().__init__(
            payload_type=payload_type,
            ssrc=ssrc,
            clock_rate=clock_rate,
            cname=cname,
            rtcp_interval=rtcp_interval,
            nack_retransmit=nack_retransmit,
            symmetric_rtp=symmetric_rtp,
            port_allocator=port_allocator,
        )
        self._codec = codec
        self._dtmf_payload_type = dtmf_payload_type

        # Receiver
        self._jitter_buffer = JitterBuffer(
            capacity=jitter_capacity,
            prefetch=jitter_prefetch,
            skip_audio_gaps=skip_audio_gaps,
        )

        # Packet loss concealment — effective when the jitter buffer skips
        # confirmed-lost packets (skip_audio_gaps) and a codec is configured.
        self._concealer = (
            PcmConcealer(sample_rate=codec.sample_rate) if plc and codec is not None else None
        )
        self._concealed_frames = 0

        # Callbacks
        self.on_audio: Callable[[bytes, int], None] | None = None
        self.on_dtmf: Callable[[str, int], None] | None = None

        # Dispatch indirection so on_dtmf can be assigned after packets arrive
        self._dtmf_receiver = DtmfReceiver(self._dispatch_dtmf)

        # DTMF sender (set during create)
        self._dtmf_sender: DtmfSender | None = None

    def _dispatch_dtmf(self, digit: str, duration: int) -> None:
        if self.on_dtmf is not None:
            self.on_dtmf(digit, duration)

    @classmethod
    async def create(
        cls,
        local_addr: tuple[str, int],
        remote_addr: tuple[str, int],
        payload_type: int,
        codec: Codec | None = None,
        ssrc: int | None = None,
        clock_rate: int = 8000,
        dtmf_payload_type: int = 101,
        cname: str = "aiortp",
        rtcp_interval: float = 5.0,
        jitter_capacity: int = 16,
        jitter_prefetch: int = 4,
        skip_audio_gaps: bool = False,
        plc: bool = True,
        nack_retransmit: bool = False,
        symmetric_rtp: bool = False,
        port_allocator: PortAllocator | None = None,
    ) -> "RTPSession":
        """Async factory to create and bind an RTP session."""
        if codec is None:
            codec = get_codec(payload_type)

        session = cls(
            payload_type=payload_type,
            codec=codec,
            ssrc=ssrc,
            clock_rate=clock_rate,
            dtmf_payload_type=dtmf_payload_type,
            cname=cname,
            rtcp_interval=rtcp_interval,
            jitter_capacity=jitter_capacity,
            jitter_prefetch=jitter_prefetch,
            skip_audio_gaps=skip_audio_gaps,
            plc=plc,
            nack_retransmit=nack_retransmit,
            symmetric_rtp=symmetric_rtp,
            port_allocator=port_allocator,
        )
        await session._bind_transports(local_addr, remote_addr)

        # Create DTMF sender (needs the RtpSender from _bind_transports)
        session._dtmf_sender = DtmfSender(
            sender=session._sender,
            dtmf_payload_type=dtmf_payload_type,
            clock_rate=clock_rate,
        )

        # Configure auto-timestamp from codec
        if session._codec is not None and session._sender is not None:
            session._sender.timestamp_increment = session._codec.samples_per_frame

        return session

    def _handle_rtp(self, data: bytes, addr: tuple[str, int]) -> None:
        """Handle incoming RTP packet."""
        try:
            packet = RtpPacket.parse(data)
        except ValueError:
            header = data[:20].hex() if len(data) >= 20 else data.hex()
            logger.warning("Failed to parse RTP packet: len=%d header=%s", len(data), header)
            return

        self._latch_rtp_addr(addr)

        # Check for DTMF
        if packet.payload_type == self._dtmf_payload_type:
            self._dtmf_receiver.handle_packet(packet)
            return

        # Learn remote SSRC from first media packet, relatch on change
        if packet.ssrc != self._remote_ssrc:
            self._remote_ssrc_changed(packet.ssrc)
            self._jitter_buffer.reset()

        assert self._stream_stats is not None
        self._stream_stats.add(packet)

        # Add to jitter buffer
        pli_flag, frame = self._jitter_buffer.add(packet)

        if frame is not None and self.on_audio is not None:
            # Decode if we have a codec
            audio_data = frame.data
            if self._codec is not None:
                try:
                    audio_data = self._codec.decode(frame.data)
                except Exception:
                    logger.warning("Failed to decode audio frame")
                    return
                if self._concealer is not None:
                    if frame.lost:
                        self._conceal_lost(frame.lost, frame.timestamp)
                    self._concealer.update(audio_data)
            self.on_audio(audio_data, frame.timestamp)

    def _conceal_lost(self, lost: int, next_timestamp: int) -> None:
        """Deliver concealment PCM covering *lost* packets before *next_timestamp*.

        The lost duration is estimated from the last decoded frame, which
        sidesteps codec clock-rate quirks (G.722).  Codec-native PLC is
        preferred; the generic concealer covers the rest.
        """
        assert self._concealer is not None and self._codec is not None
        assert self.on_audio is not None
        samples = lost * self._concealer.frame_samples
        if samples == 0:
            return  # no decoded frame yet to anchor concealment
        pcm = self._codec.conceal(samples)
        if pcm is None:
            pcm = self._concealer.conceal(samples)
        self._concealed_frames += lost
        timestamp = (next_timestamp - lost * self._codec.samples_per_frame) & 0xFFFFFFFF
        self.on_audio(pcm, timestamp)

    def _handle_rtcp(self, data: bytes, addr: tuple[str, int]) -> None:
        """Handle incoming RTCP packet."""
        try:
            packets = RtcpPacket.parse(data)
        except ValueError:
            logger.warning("Failed to parse RTCP packet")
            return

        self._latch_rtcp_addr(addr)

        for packet in packets:
            if isinstance(packet, RtcpSrPacket):
                self._record_incoming_sr(packet.sender_info.ntp_timestamp)
                self._process_receiver_reports(packet.reports)
            elif isinstance(packet, RtcpRrPacket):
                self._process_receiver_reports(packet.reports)
            elif isinstance(packet, RtcpRtpfbPacket) and packet.fmt == RTCP_RTPFB_NACK:
                self._handle_incoming_nack(packet)
            elif isinstance(packet, RtcpByePacket):
                logger.info("Received RTCP BYE from %s", packet.sources)

    def send_audio(self, payload: bytes, timestamp: int) -> None:
        """Send encoded audio payload (already codec-encoded)."""
        if self._sender is None or self._closed:
            return
        self._sender.send_frame(payload, timestamp, addr=self._remote_addr)

    def send_audio_auto(self, payload: bytes) -> int:
        """Send encoded audio with auto-incrementing timestamp.

        Returns the RTP timestamp used.
        """
        if self._sender is None or self._closed:
            return 0
        return self._sender.send_frame_auto(payload, addr=self._remote_addr)

    def send_audio_pcm(self, pcm: bytes, timestamp: int) -> None:
        """Send raw PCM audio, encoding with session codec."""
        if self._codec is None:
            raise RuntimeError("No codec configured for PCM encoding")
        encoded = self._codec.encode(pcm)
        self.send_audio(encoded, timestamp)

    def send_audio_pcm_auto(self, pcm: bytes) -> int:
        """Encode and send PCM audio with auto-incrementing timestamp.

        Returns the RTP timestamp used.
        """
        if self._codec is None:
            raise RuntimeError("No codec configured for PCM encoding")
        encoded = self._codec.encode(pcm)
        return self.send_audio_auto(encoded)

    def send_dtmf(self, digit: str, duration_ms: int = 160, timestamp: int = 0) -> None:
        """Send a DTMF digit."""
        if self._dtmf_sender is None or self._closed:
            return
        self._dtmf_sender.send_digit(
            digit, duration_ms, timestamp=timestamp, addr=self._remote_addr
        )

    @property
    def codec(self) -> Codec | None:
        """The codec used by this session, or ``None`` if not configured."""
        return self._codec

    @property
    def stats(self) -> dict[str, Any]:
        """Session statistics, including receiver-side concealment."""
        result = super().stats
        result["concealed_frames"] = self._concealed_frames
        return result
