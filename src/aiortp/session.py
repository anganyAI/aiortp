"""RTPSession — main orchestrator for sending/receiving RTP audio."""

import asyncio
import logging
import random
from collections.abc import Callable
from typing import Any, Optional

from . import clock
from .codecs import Codec, get_codec
from .dtmf import DtmfReceiver, DtmfSender
from .jitterbuffer import JitterBuffer
from .packet import (
    RtcpByePacket,
    RtcpPacket,
    RtcpSdesPacket,
    RtcpSenderInfo,
    RtcpSourceInfo,
    RtcpSrPacket,
    RtpPacket,
)
from .sender import RtpSender
from .stats import StreamStatistics
from .transport import RtpTransport
from .utils import random32

logger = logging.getLogger(__name__)


class RTPSession:
    def __init__(
        self,
        payload_type: int,
        codec: Optional[Codec] = None,
        ssrc: Optional[int] = None,
        clock_rate: int = 8000,
        dtmf_payload_type: int = 101,
        cname: str = "aiortp",
        rtcp_interval: float = 5.0,
    ) -> None:
        self._payload_type = payload_type
        self._codec = codec
        self._ssrc = ssrc if ssrc is not None else random32()
        self._clock_rate = clock_rate
        self._dtmf_payload_type = dtmf_payload_type
        self._cname = cname
        self._rtcp_interval = rtcp_interval

        # Transport
        self._rtp_transport: Optional[RtpTransport] = None
        self._rtcp_transport: Optional[RtpTransport] = None
        self._remote_addr: Optional[tuple[str, int]] = None
        self._remote_rtcp_addr: Optional[tuple[str, int]] = None

        # Sender
        self._sender: Optional[RtpSender] = None
        self._dtmf_sender: Optional[DtmfSender] = None

        # Receiver
        self._jitter_buffer = JitterBuffer(capacity=16, prefetch=4)
        self._stream_stats: Optional[StreamStatistics] = None

        # RTCP
        self._rtcp_task: Optional[asyncio.Task[None]] = None
        self._last_sr_ntp: Optional[int] = None
        self._last_sr_rtp_ts: Optional[int] = None

        # Callbacks
        self.on_audio: Optional[Callable[[bytes, int], None]] = None
        self.on_dtmf: Optional[Callable[[str, int], None]] = None

        # State
        self._closed = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None

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
        )
        session._loop = asyncio.get_running_loop()
        session._remote_addr = remote_addr
        if remote_addr[1] == 0:
            session._remote_rtcp_addr = remote_addr
        else:
            session._remote_rtcp_addr = (remote_addr[0], remote_addr[1] + 1)

        # Create RTP transport
        rtp_transport_obj = RtpTransport(
            on_rtp=session._handle_rtp,
            on_rtcp=session._handle_rtcp,
        )
        _, rtp_protocol = await session._loop.create_datagram_endpoint(
            lambda: rtp_transport_obj,
            local_addr=local_addr,
        )
        session._rtp_transport = rtp_transport_obj

        # Create RTCP transport
        # Get the actual bound RTP port to determine RTCP port
        rtp_bound = rtp_transport_obj._transport.get_extra_info("sockname")  # type: ignore[union-attr]
        if local_addr[1] == 0:
            # When using port 0 (OS-assigned), also let OS assign RTCP port
            rtcp_local = (local_addr[0], 0)
        else:
            rtcp_local = (local_addr[0], rtp_bound[1] + 1)
        rtcp_transport_obj = RtpTransport(
            on_rtp=session._handle_rtp,
            on_rtcp=session._handle_rtcp,
        )
        _, rtcp_protocol = await session._loop.create_datagram_endpoint(
            lambda: rtcp_transport_obj,
            local_addr=rtcp_local,
        )
        session._rtcp_transport = rtcp_transport_obj

        # Create sender
        session._sender = RtpSender(
            transport=session._rtp_transport,
            payload_type=payload_type,
            ssrc=session._ssrc,
            clock_rate=clock_rate,
        )

        # Create DTMF sender
        session._dtmf_sender = DtmfSender(
            sender=session._sender,
            dtmf_payload_type=dtmf_payload_type,
            clock_rate=clock_rate,
        )

        # Start RTCP loop
        session._rtcp_task = asyncio.create_task(session._run_rtcp())

        return session

    def _handle_rtp(self, data: bytes) -> None:
        """Handle incoming RTP packet."""
        try:
            packet = RtpPacket.parse(data)
        except ValueError:
            logger.warning("Failed to parse RTP packet")
            return

        # Check for DTMF
        if packet.payload_type == self._dtmf_payload_type:
            if self._dtmf_receiver is not None:
                self._dtmf_receiver.handle_packet(packet)
            return

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
                self._last_sr_ntp = packet.sender_info.ntp_timestamp
                self._last_sr_rtp_ts = packet.sender_info.rtp_timestamp
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

    def send_audio_pcm(self, pcm: bytes, timestamp: int) -> None:
        """Send raw PCM audio, encoding with session codec."""
        if self._codec is None:
            raise RuntimeError("No codec configured for PCM encoding")
        encoded = self._codec.encode(pcm)
        self.send_audio(encoded, timestamp)

    def send_dtmf(
        self, digit: str, duration_ms: int = 160, timestamp: int = 0
    ) -> None:
        """Send a DTMF digit."""
        if self._dtmf_sender is None or self._closed:
            return
        self._dtmf_sender.send_digit(
            digit, duration_ms, timestamp=timestamp, addr=self._remote_addr
        )

    def update_remote(self, addr: tuple[str, int]) -> None:
        """Update remote address (e.g., for re-INVITE)."""
        self._remote_addr = addr
        self._remote_rtcp_addr = (addr[0], addr[1] + 1)

    async def _run_rtcp(self) -> None:
        """Periodic RTCP sender loop."""
        try:
            while not self._closed:
                # RFC 3550 randomized interval
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

        ntp_time = clock.current_ntp_time()

        sr = RtcpSrPacket(
            ssrc=self._ssrc,
            sender_info=RtcpSenderInfo(
                ntp_timestamp=ntp_time,
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

        data = bytes(sr) + bytes(sdes)
        self._rtcp_transport.send(data, self._remote_rtcp_addr)

    def _send_bye(self) -> None:
        """Send RTCP BYE packet."""
        if self._rtcp_transport is None:
            return

        bye = RtcpByePacket(sources=[self._ssrc])
        self._rtcp_transport.send(bytes(bye), self._remote_rtcp_addr)

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

    async def close(self) -> None:
        """Close the session, sending BYE and releasing resources."""
        if self._closed:
            return
        self._closed = True

        # Cancel RTCP task
        if self._rtcp_task is not None:
            self._rtcp_task.cancel()
            try:
                await self._rtcp_task
            except asyncio.CancelledError:
                pass

        # Send BYE
        self._send_bye()

        # Allow BYE packet to be sent
        await asyncio.sleep(0)

        # Close transports
        if self._rtp_transport is not None:
            self._rtp_transport.close()
        if self._rtcp_transport is not None:
            self._rtcp_transport.close()
