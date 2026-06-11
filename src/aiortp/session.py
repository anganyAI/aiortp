"""RTPSession — main orchestrator for sending/receiving RTP audio."""

import logging
from collections.abc import Callable
from typing import Any

from .base_session import BaseRTPSession
from .clock import MediaClock
from .codecs import Codec, get_codec
from .dtmf import DtmfReceiver, DtmfSender
from .jitterbuffer import JitterBuffer, JitterFrame
from .pacer import PacedSender
from .packet import (
    RTCP_RTPFB_NACK,
    RtcpByePacket,
    RtcpPacket,
    RtcpRrPacket,
    RtcpRtpfbPacket,
    RtcpSrPacket,
    RtpPacket,
)
from .playout import AdaptivePlayout
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
        playout: bool = False,
        playout_max_delay_ms: int = 200,
        paced: bool = False,
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
        if (playout or paced) and codec is None:
            raise RuntimeError("playout and paced modes require a codec")
        self._codec = codec
        self._dtmf_payload_type = dtmf_payload_type

        # Receiver — in playout mode the buffering depth lives in
        # AdaptivePlayout; the jitter buffer only reorders and assembles.
        self._playout = (
            AdaptivePlayout(codec, max_delay_ms=playout_max_delay_ms)
            if playout and codec is not None
            else None
        )
        self._playout_clock: MediaClock | None = None
        self._jitter_buffer = JitterBuffer(
            capacity=jitter_capacity,
            prefetch=0 if playout else jitter_prefetch,
            skip_audio_gaps=skip_audio_gaps or playout,
        )

        # Packet loss concealment for the arrival-driven path — in playout
        # mode concealment is deadline-based inside AdaptivePlayout instead.
        self._concealer = (
            PcmConcealer(sample_rate=codec.sample_rate)
            if plc and codec is not None and not playout
            else None
        )
        self._concealed_frames = 0

        # Paced sending (instantiated in create, needs the RtpSender)
        self._paced = paced
        self._paced_sender: PacedSender | None = None
        self._pacer_clock: MediaClock | None = None

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
        playout: bool = False,
        playout_max_delay_ms: int = 200,
        paced: bool = False,
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
            playout=playout,
            playout_max_delay_ms=playout_max_delay_ms,
            paced=paced,
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

        # Media clocks (require a running loop)
        if session._playout is not None:
            session._playout_clock = MediaClock(session._playout.ptime, session._playout_tick)
            session._playout_clock.start()
        if paced and session._sender is not None:
            session._paced_sender = PacedSender(
                session._sender,
                get_addr=lambda: session._remote_addr,
            )
            session._pacer_clock = MediaClock(
                codec.samples_per_frame / codec.sample_rate,
                session._paced_sender.tick,
            )
            session._pacer_clock.start()

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
            # Telephone-events consume audio sequence numbers: mark the
            # slot so the jitter buffer never reads it as packet loss.
            # The marker may complete a pending pre-digit frame.
            frame = self._jitter_buffer.mark_non_media(packet.sequence_number)
            if self._playout is not None:
                # RFC 4733 packets replace audio while a digit is sent —
                # sender suppression, not loss
                self._playout.suppress()
            elif frame is not None:
                self._deliver_audio_frame(frame)
            self._dtmf_receiver.handle_packet(packet)
            return

        # Learn remote SSRC from first media packet, relatch on change
        if packet.ssrc != self._remote_ssrc:
            self._remote_ssrc_changed(packet.ssrc)
            self._jitter_buffer.reset()
            if self._playout is not None:
                self._playout.reset()

        assert self._stream_stats is not None
        self._stream_stats.add(packet)

        # Add to jitter buffer
        pli_flag, frame = self._jitter_buffer.add(packet)
        if frame is not None:
            self._deliver_audio_frame(frame)

    def _deliver_audio_frame(self, frame: JitterFrame) -> None:
        """Route an assembled frame to the playout buffer or on_audio."""
        if self._playout is not None:
            self._playout.put(frame.timestamp, frame.data)
            return
        if self.on_audio is None:
            return

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

    def _playout_tick(self) -> None:
        """Deliver one ptime of audio from the adaptive playout buffer."""
        assert self._playout is not None
        frame = self._playout.tick()
        if frame is not None and self.on_audio is not None:
            self.on_audio(frame[0], frame[1])

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
        if self._paced_sender is not None:
            raise RuntimeError("paced mode owns timestamps; use send_audio_auto")
        if self._sender is None or self._closed:
            return
        self._sender.send_frame(payload, timestamp, addr=self._remote_addr)

    def send_audio_auto(self, payload: bytes) -> int:
        """Send encoded audio with auto-incrementing timestamp.

        Returns the RTP timestamp used, or 0 in paced mode where the
        pacer assigns timestamps at transmission time.
        """
        if self._sender is None or self._closed:
            return 0
        if self._paced_sender is not None:
            self._paced_sender.enqueue(payload)
            return 0
        return self._sender.send_frame_auto(payload, addr=self._remote_addr)

    async def drain(self) -> None:
        """Wait until the paced send queue is fully transmitted (no-op unpaced)."""
        if self._paced_sender is not None:
            await self._paced_sender.drain()

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

    async def close(self) -> None:
        """Stop the media clocks, then close transports and RTCP."""
        if self._playout_clock is not None:
            await self._playout_clock.stop()
        if self._pacer_clock is not None:
            await self._pacer_clock.stop()
        await super().close()

    @property
    def stats(self) -> dict[str, Any]:
        """Session statistics, including receiver-side concealment."""
        result = super().stats
        result["concealed_frames"] = self._concealed_frames
        if self._playout is not None:
            result.update(self._playout.stats())
        if self._paced_sender is not None:
            result.update(self._paced_sender.stats())
        return result
