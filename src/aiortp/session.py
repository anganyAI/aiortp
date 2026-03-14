"""RTPSession — main orchestrator for sending/receiving RTP audio."""

import logging
from collections.abc import Callable
from typing import Optional

from .base_session import BaseRTPSession
from .codecs import Codec, get_codec
from .port_allocator import PortAllocator
from .dtmf import DtmfReceiver, DtmfSender
from .jitterbuffer import JitterBuffer
from .packet import (
    RtcpByePacket,
    RtcpPacket,
    RtcpSrPacket,
    RtpPacket,
)
from .stats import StreamStatistics

logger = logging.getLogger(__name__)


class RTPSession(BaseRTPSession):
    def __init__(
        self,
        payload_type: int,
        codec: Optional[Codec] = None,
        ssrc: Optional[int] = None,
        clock_rate: int = 8000,
        dtmf_payload_type: int = 101,
        cname: str = "aiortp",
        rtcp_interval: float = 5.0,
        jitter_capacity: int = 16,
        jitter_prefetch: int = 4,
        skip_audio_gaps: bool = False,
        port_allocator: Optional[PortAllocator] = None,
    ) -> None:
        super().__init__(
            payload_type=payload_type,
            ssrc=ssrc,
            clock_rate=clock_rate,
            cname=cname,
            rtcp_interval=rtcp_interval,
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

        # Callbacks
        self.on_audio: Optional[Callable[[bytes, int], None]] = None
        self.on_dtmf: Optional[Callable[[str, int], None]] = None

        # DTMF sender (set during create)
        self._dtmf_sender: Optional[DtmfSender] = None

    @classmethod
    async def create(
        cls,
        local_addr: tuple[str, int],
        remote_addr: tuple[str, int],
        payload_type: int,
        codec: Optional[Codec] = None,
        ssrc: Optional[int] = None,
        clock_rate: int = 8000,
        dtmf_payload_type: int = 101,
        cname: str = "aiortp",
        rtcp_interval: float = 5.0,
        jitter_capacity: int = 16,
        jitter_prefetch: int = 4,
        skip_audio_gaps: bool = False,
        port_allocator: Optional[PortAllocator] = None,
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

    def _handle_rtp(self, data: bytes) -> None:
        """Handle incoming RTP packet."""
        try:
            packet = RtpPacket.parse(data)
        except ValueError:
            header = data[:20].hex() if len(data) >= 20 else data.hex()
            logger.warning(
                "Failed to parse RTP packet: len=%d header=%s", len(data), header
            )
            return

        # Check for DTMF
        if packet.payload_type == self._dtmf_payload_type:
            if self._dtmf_receiver is not None:
                self._dtmf_receiver.handle_packet(packet)
            return

        # Learn remote SSRC from first media packet
        if self._remote_ssrc is None:
            self._remote_ssrc = packet.ssrc

        # Initialize stream stats on first packet
        if self._stream_stats is None:
            self._stream_stats = StreamStatistics(clockrate=self._clock_rate)

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
            self.on_audio(audio_data, frame.timestamp)

    def _handle_rtcp(self, data: bytes) -> None:
        """Handle incoming RTCP packet."""
        try:
            packets = RtcpPacket.parse(data)
        except ValueError:
            logger.warning("Failed to parse RTCP packet")
            return

        for packet in packets:
            if isinstance(packet, RtcpSrPacket):
                self._record_incoming_sr(packet.sender_info.ntp_timestamp)
            elif isinstance(packet, RtcpByePacket):
                logger.info("Received RTCP BYE from %s", packet.sources)

    @property
    def _dtmf_receiver(self) -> Optional[DtmfReceiver]:
        if not hasattr(self, "_dtmf_receiver_instance"):
            if self.on_dtmf is not None:
                self._dtmf_receiver_instance: Optional[DtmfReceiver] = DtmfReceiver(
                    self.on_dtmf
                )
            else:
                self._dtmf_receiver_instance = None
        return self._dtmf_receiver_instance

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

    def send_dtmf(
        self, digit: str, duration_ms: int = 160, timestamp: int = 0
    ) -> None:
        """Send a DTMF digit."""
        if self._dtmf_sender is None or self._closed:
            return
        self._dtmf_sender.send_digit(
            digit, duration_ms, timestamp=timestamp, addr=self._remote_addr
        )

    @property
    def codec(self) -> Optional[Codec]:
        """The codec used by this session, or ``None`` if not configured."""
        return self._codec
