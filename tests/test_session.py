import asyncio
import struct

import pytest

from aiortp.codecs import get_codec
from aiortp.dtmf import DtmfEvent
from aiortp.packet import (
    RTCP_RTPFB_NACK,
    RtcpPacket,
    RtcpReceiverInfo,
    RtcpRrPacket,
    RtcpRtpfbPacket,
    RtcpSrPacket,
    RtpPacket,
)
from aiortp.sender import RtpSender
from aiortp.session import RTPSession
from aiortp.transport import RtpTransport

# Dummy source address for direct _handle_rtp/_handle_rtcp injection
_ADDR = ("198.51.100.1", 5004)


def _plc_session(plc: bool = True) -> RTPSession:
    """Session without transports — packets injected via _handle_rtp."""
    return RTPSession(
        payload_type=0,
        codec=get_codec(0),
        jitter_prefetch=0,
        skip_audio_gaps=True,
        plc=plc,
    )


def _inject_pcmu(session: RTPSession, sequences: tuple[int, ...]) -> None:
    """Inject PCMU packets with 20 ms spacing; missing sequences are lost."""
    payload = get_codec(0).encode(struct.pack("<160h", *([1000] * 160)))
    for seq in sequences:
        packet = RtpPacket(
            payload_type=0, sequence_number=seq, timestamp=seq * 160, payload=payload
        )
        session._handle_rtp(packet.serialize(), _ADDR)


def test_plc_inserts_concealment_on_loss() -> None:
    """Lost packets are replaced by concealment PCM, keeping the timeline continuous."""
    session = _plc_session()
    received: list[tuple[bytes, int]] = []
    session.on_audio = lambda pcm, ts: received.append((pcm, ts))

    # seq2 and seq3 lost
    _inject_pcmu(session, (0, 1, 4, 5))

    assert [ts for _, ts in received] == [0, 160, 320, 640]
    concealed = received[2][0]
    assert len(concealed) == 640  # 2 lost packets × 160 samples × 2 bytes
    # Concealment repeats the last frame (faded), not silence
    first_sample = struct.unpack_from("<h", concealed)[0]
    assert abs(first_sample) > 500
    # Delivered sample count covers the full timeline, loss included
    assert sum(len(pcm) for pcm, _ in received) == 5 * 320
    assert session.stats["concealed_frames"] == 2


def test_plc_disabled_skips_lost_audio() -> None:
    """With plc=False the lost duration simply disappears from the stream."""
    session = _plc_session(plc=False)
    received: list[tuple[bytes, int]] = []
    session.on_audio = lambda pcm, ts: received.append((pcm, ts))

    _inject_pcmu(session, (0, 1, 4, 5))

    assert [ts for _, ts in received] == [0, 160, 640]
    assert session.stats["concealed_frames"] == 0


@pytest.mark.asyncio
async def test_loopback_raw() -> None:
    """Two sessions exchange raw payloads on localhost."""
    received: list[tuple[bytes, int]] = []
    event = asyncio.Event()

    session_a = await RTPSession.create(
        local_addr=("127.0.0.1", 0),
        remote_addr=("127.0.0.1", 0),  # will update after binding
        payload_type=0,
        rtcp_interval=60.0,  # effectively disable RTCP for this test
    )

    # Get the actual bound port for session A
    rtp_a_addr = session_a._rtp_transport._transport.get_extra_info("sockname")  # type: ignore[union-attr]

    session_b = await RTPSession.create(
        local_addr=("127.0.0.1", 0),
        remote_addr=(rtp_a_addr[0], rtp_a_addr[1]),
        payload_type=0,
        rtcp_interval=60.0,
    )

    rtp_b_addr = session_b._rtp_transport._transport.get_extra_info("sockname")  # type: ignore[union-attr]

    # Update session A to point to session B
    session_a.update_remote((rtp_b_addr[0], rtp_b_addr[1]))

    def on_audio(data: bytes, timestamp: int) -> None:
        received.append((data, timestamp))
        if len(received) >= 1:
            event.set()

    session_b.on_audio = on_audio

    # Send enough packets to fill the jitter buffer prefetch (4 frames)
    for i in range(6):
        payload = bytes([i]) * 160
        session_a.send_audio(payload, timestamp=i * 160)

    try:
        await asyncio.wait_for(event.wait(), timeout=2.0)
    except TimeoutError:
        pass

    # Verify at least one frame was received
    assert len(received) >= 1
    # The first received frame should be from the first packet
    assert received[0][1] == 0  # timestamp of first frame

    await session_a.close()
    await session_b.close()


@pytest.mark.asyncio
async def test_stats() -> None:
    """Verify stats are populated after sending packets."""
    session = await RTPSession.create(
        local_addr=("127.0.0.1", 0),
        remote_addr=("127.0.0.1", 19999),
        payload_type=0,
        rtcp_interval=60.0,
    )

    for i in range(5):
        session.send_audio(b"\x00" * 160, timestamp=i * 160)

    stats = session.stats
    assert stats["packets_sent"] == 5
    assert stats["octets_sent"] == 5 * 160
    assert stats["ssrc"] == session._ssrc

    await session.close()


@pytest.mark.asyncio
async def test_rtcp_bye_on_close() -> None:
    """Verify BYE is sent on close."""
    session_a = await RTPSession.create(
        local_addr=("127.0.0.1", 0),
        remote_addr=("127.0.0.1", 0),
        payload_type=0,
        rtcp_interval=60.0,
    )

    rtp_a_addr = session_a._rtp_transport._transport.get_extra_info("sockname")  # type: ignore[union-attr]

    session_b = await RTPSession.create(
        local_addr=("127.0.0.1", 0),
        remote_addr=(rtp_a_addr[0], rtp_a_addr[1]),
        payload_type=0,
        rtcp_interval=60.0,
    )

    rtp_b_addr = session_b._rtp_transport._transport.get_extra_info("sockname")  # type: ignore[union-attr]

    session_a.update_remote((rtp_b_addr[0], rtp_b_addr[1]))

    # Close session A - should send BYE
    await session_a.close()
    # Give time for BYE to arrive
    await asyncio.sleep(0.1)

    await session_b.close()


@pytest.mark.asyncio
async def test_send_audio_auto_increments_timestamp() -> None:
    """Auto-timestamp increments by samples_per_frame."""
    session = await RTPSession.create(
        local_addr=("127.0.0.1", 0),
        remote_addr=("127.0.0.1", 19999),
        payload_type=0,  # PCMU: 160 samples/frame
        rtcp_interval=60.0,
    )

    ts1 = session.send_audio_auto(b"\x00" * 160)
    ts2 = session.send_audio_auto(b"\x00" * 160)
    assert ts2 == (ts1 + 160) & 0xFFFFFFFF
    assert session.stats["packets_sent"] == 2

    await session.close()


@pytest.mark.asyncio
async def test_sr_contains_real_rtp_timestamp() -> None:
    """SR rtp_timestamp reflects the last sent RTP timestamp, not 0."""
    sent_rtcp: list[bytes] = []

    session = await RTPSession.create(
        local_addr=("127.0.0.1", 0),
        remote_addr=("127.0.0.1", 19999),
        payload_type=0,
        rtcp_interval=60.0,
    )

    # Mock RTCP transport to capture outbound SR
    session._rtcp_transport.send = lambda data, addr=None: sent_rtcp.append(data)  # type: ignore[union-attr, assignment]

    # Send audio so last_rtp_timestamp is populated
    session.send_audio(b"\x00" * 160, timestamp=12345)

    # Trigger SR manually
    session._send_rtcp_report()

    assert len(sent_rtcp) >= 1
    packets = RtcpPacket.parse(sent_rtcp[0])
    sr = next(p for p in packets if isinstance(p, RtcpSrPacket))
    assert sr.sender_info.rtp_timestamp == 12345

    await session.close()


@pytest.mark.asyncio
async def test_rr_sent_when_receiving() -> None:
    """Receiver report is included in SR when we have inbound stats."""
    session_a = await RTPSession.create(
        local_addr=("127.0.0.1", 0),
        remote_addr=("127.0.0.1", 0),
        payload_type=0,
        rtcp_interval=60.0,
    )
    a_addr = session_a._rtp_transport._transport.get_extra_info("sockname")  # type: ignore[union-attr]

    session_b = await RTPSession.create(
        local_addr=("127.0.0.1", 0),
        remote_addr=a_addr,
        payload_type=0,
        rtcp_interval=60.0,
    )
    b_addr = session_b._rtp_transport._transport.get_extra_info("sockname")  # type: ignore[union-attr]
    session_a.update_remote(b_addr)

    # A sends packets to B so B has stream stats
    for i in range(5):
        session_a.send_audio(b"\x00" * 160, timestamp=i * 160)
    await asyncio.sleep(0.1)

    # Capture B's RTCP output
    sent_rtcp: list[bytes] = []
    session_b._rtcp_transport.send = lambda data, addr=None: sent_rtcp.append(data)  # type: ignore[union-attr, assignment]

    # B also sends so it produces SR (not just RR)
    session_b.send_audio(b"\x00" * 160, timestamp=0)
    session_b._send_rtcp_report()

    assert len(sent_rtcp) >= 1
    packets = RtcpPacket.parse(sent_rtcp[0])
    sr = next(p for p in packets if isinstance(p, RtcpSrPacket))
    # SR should contain a receiver report block
    assert len(sr.reports) == 1
    assert sr.reports[0].ssrc == session_a._ssrc

    await session_a.close()
    await session_b.close()


@pytest.mark.asyncio
async def test_incoming_rr_processed() -> None:
    """Incoming RR updates stats and fires callback."""
    session = await RTPSession.create(
        local_addr=("127.0.0.1", 0),
        remote_addr=("127.0.0.1", 19999),
        payload_type=0,
        rtcp_interval=60.0,
    )

    received_rr: list[RtcpReceiverInfo] = []
    session.on_receiver_report = lambda rr: received_rr.append(rr)

    # Simulate receiving an RR that reports on our SSRC
    rr = RtcpRrPacket(
        ssrc=99999,
        reports=[
            RtcpReceiverInfo(
                ssrc=session._ssrc,
                fraction_lost=25,
                packets_lost=10,
                highest_sequence=500,
                jitter=3,
                lsr=0,
                dlsr=0,
            )
        ],
    )
    session._handle_rtcp(bytes(rr), _ADDR)

    assert len(received_rr) == 1
    assert received_rr[0].fraction_lost == 25

    stats = session.stats
    assert stats["remote_fraction_lost"] == 25
    assert stats["remote_packets_lost"] == 10
    assert stats["remote_jitter"] == 3

    await session.close()


@pytest.mark.asyncio
async def test_incoming_rr_in_sr_processed() -> None:
    """RR blocks embedded in SR are also processed."""
    from aiortp.packet import RtcpSenderInfo

    session = await RTPSession.create(
        local_addr=("127.0.0.1", 0),
        remote_addr=("127.0.0.1", 19999),
        payload_type=0,
        rtcp_interval=60.0,
    )

    sr = RtcpSrPacket(
        ssrc=88888,
        sender_info=RtcpSenderInfo(
            ntp_timestamp=1000 << 32,
            rtp_timestamp=0,
            packet_count=50,
            octet_count=8000,
        ),
        reports=[
            RtcpReceiverInfo(
                ssrc=session._ssrc,
                fraction_lost=50,
                packets_lost=20,
                highest_sequence=1000,
                jitter=7,
                lsr=0,
                dlsr=0,
            )
        ],
    )
    session._handle_rtcp(bytes(sr), _ADDR)

    stats = session.stats
    assert stats["remote_fraction_lost"] == 50
    assert stats["remote_jitter"] == 7

    await session.close()


class TestNackRetransmission:
    def test_sender_retransmits_from_history(self) -> None:
        """Sender retransmits packets that are in the history buffer."""
        sent: list[bytes] = []
        transport = RtpTransport(on_rtp=lambda d, a: None, on_rtcp=lambda d, a: None)
        transport.send = lambda data, addr=None: sent.append(data)  # type: ignore[assignment]

        sender = RtpSender(transport=transport, payload_type=0, ssrc=1000)
        initial_seq = sender.sequence_number

        # Send 5 packets
        for i in range(5):
            sender.send_frame(b"\x00" * 160, timestamp=i * 160)
        assert len(sent) == 5

        # Retransmit packets 2 and 4 (by sequence number)
        seq2 = (initial_seq + 2) & 0xFFFF
        seq4 = (initial_seq + 4) & 0xFFFF
        count = sender.retransmit([seq2, seq4])
        assert count == 2
        assert len(sent) == 7  # 5 original + 2 retransmitted

        # Retransmitted data matches original
        assert sent[5] == sent[2]  # seq2
        assert sent[6] == sent[4]  # seq4

    def test_sender_skips_missing_history(self) -> None:
        """Retransmit returns 0 for packets not in history."""
        sent: list[bytes] = []
        transport = RtpTransport(on_rtp=lambda d, a: None, on_rtcp=lambda d, a: None)
        transport.send = lambda data, addr=None: sent.append(data)  # type: ignore[assignment]

        sender = RtpSender(transport=transport, payload_type=0, ssrc=1000)
        count = sender.retransmit([9999])
        assert count == 0

    @pytest.mark.asyncio
    async def test_session_handles_incoming_nack(self) -> None:
        """Session retransmits when receiving a NACK."""
        session_a = await RTPSession.create(
            local_addr=("127.0.0.1", 0),
            remote_addr=("127.0.0.1", 0),
            payload_type=0,
            rtcp_interval=60.0,
            nack_retransmit=True,
        )
        a_addr = session_a._rtp_transport._transport.get_extra_info("sockname")  # type: ignore[union-attr]

        session_b = await RTPSession.create(
            local_addr=("127.0.0.1", 0),
            remote_addr=a_addr,
            payload_type=0,
            rtcp_interval=60.0,
        )
        b_addr = session_b._rtp_transport._transport.get_extra_info("sockname")  # type: ignore[union-attr]
        session_a.update_remote(b_addr)

        # A sends packets to B
        initial_seq = session_a._sender.sequence_number  # type: ignore[union-attr]
        for i in range(5):
            session_a.send_audio(b"\x00" * 160, timestamp=i * 160)

        packets_before = session_a._sender.packets_sent  # type: ignore[union-attr]

        # B sends NACK to A requesting retransmission of seq+1
        nack_seq = (initial_seq + 1) & 0xFFFF
        nack = RtcpRtpfbPacket(
            fmt=RTCP_RTPFB_NACK,
            ssrc=session_b._ssrc,
            media_ssrc=session_a._ssrc,
            lost=[nack_seq],
        )
        session_a._handle_rtcp(bytes(nack), _ADDR)

        # packets_sent doesn't change (retransmit doesn't increment counters)
        assert session_a._sender.packets_sent == packets_before  # type: ignore[union-attr]

        await session_a.close()
        await session_b.close()

    def test_sender_history_disabled(self) -> None:
        """With enable_history=False the sender keeps nothing to retransmit."""
        sent: list[bytes] = []
        transport = RtpTransport(on_rtp=lambda d, a: None, on_rtcp=lambda d, a: None)
        transport.send = lambda data, addr=None: sent.append(data)  # type: ignore[assignment]

        sender = RtpSender(transport=transport, payload_type=0, ssrc=1000, enable_history=False)
        initial_seq = sender.sequence_number
        for i in range(5):
            sender.send_frame(b"\x00" * 160, timestamp=i * 160)

        assert sender.retransmit([(initial_seq + 2) & 0xFFFF]) == 0
        assert len(sent) == 5  # nothing retransmitted

    @pytest.mark.asyncio
    async def test_audio_session_no_retransmit_by_default(self) -> None:
        """Audio sessions keep no NACK history unless nack_retransmit=True."""
        session = await RTPSession.create(
            local_addr=("127.0.0.1", 0),
            remote_addr=("127.0.0.1", 9),
            payload_type=0,
            rtcp_interval=60.0,
        )
        for i in range(3):
            session.send_audio(b"\x00" * 160, timestamp=i * 160)
        assert session._sender._history == {}  # type: ignore[union-attr]
        await session.close()


def test_dtmf_received_when_callback_assigned_late() -> None:
    """DTMF arriving before on_dtmf is assigned must not disable reception."""
    session = RTPSession(payload_type=0, codec=get_codec(0))

    # First digit arrives before any callback is assigned — dropped, not fatal
    ev = DtmfEvent(event=1, end=True, volume=10, duration=1280)
    pkt = RtpPacket(payload_type=101, sequence_number=100, timestamp=1000, payload=ev.serialize())
    session._handle_rtp(pkt.serialize(), _ADDR)

    received: list[tuple[str, int]] = []
    session.on_dtmf = lambda digit, duration: received.append((digit, duration))

    ev = DtmfEvent(event=2, end=True, volume=10, duration=1280)
    pkt = RtpPacket(payload_type=101, sequence_number=101, timestamp=2000, payload=ev.serialize())
    session._handle_rtp(pkt.serialize(), _ADDR)

    assert received == [("2", 1280)]


def test_remote_ssrc_change_relatches_and_resets() -> None:
    """A mid-call SSRC change relatches the source and resets inbound state."""
    session = RTPSession(payload_type=0, codec=get_codec(0), jitter_prefetch=0)
    received: list[int] = []
    session.on_audio = lambda pcm, ts: received.append(ts)

    payload = get_codec(0).encode(b"\x00\x00" * 160)
    for seq in range(3):
        pkt = RtpPacket(
            payload_type=0, sequence_number=seq, timestamp=seq * 160, ssrc=1111, payload=payload
        )
        session._handle_rtp(pkt.serialize(), _ADDR)
    assert session._remote_ssrc == 1111
    assert len(received) == 2  # two complete frames delivered

    # New source: different SSRC, unrelated sequence space
    for seq in range(5000, 5003):
        pkt = RtpPacket(
            payload_type=0, sequence_number=seq, timestamp=seq * 160, ssrc=2222, payload=payload
        )
        session._handle_rtp(pkt.serialize(), _ADDR)

    assert session._remote_ssrc == 2222
    assert session._stream_stats is not None
    assert session._stream_stats.packets_received == 3  # stats reset on change
    assert len(received) == 4  # audio keeps flowing from the new source


class TestSymmetricRtp:
    def _rtp_packet(self) -> bytes:
        return RtpPacket(
            payload_type=0, sequence_number=1, timestamp=0, ssrc=42, payload=b"\x00" * 160
        ).serialize()

    def test_latch_disabled_by_default(self) -> None:
        session = RTPSession(payload_type=0, codec=get_codec(0))
        session._remote_addr = ("10.0.0.1", 4000)
        session._handle_rtp(self._rtp_packet(), ("192.0.2.7", 12345))
        assert session._remote_addr == ("10.0.0.1", 4000)

    def test_latch_rtp_addr(self) -> None:
        session = RTPSession(payload_type=0, codec=get_codec(0), symmetric_rtp=True)
        session._remote_addr = ("10.0.0.1", 4000)
        session._handle_rtp(self._rtp_packet(), ("192.0.2.7", 12345))
        assert session._remote_addr == ("192.0.2.7", 12345)

    def test_invalid_packet_does_not_latch(self) -> None:
        session = RTPSession(payload_type=0, codec=get_codec(0), symmetric_rtp=True)
        session._remote_addr = ("10.0.0.1", 4000)
        session._handle_rtp(b"\x00\x01", ("192.0.2.7", 12345))
        assert session._remote_addr == ("10.0.0.1", 4000)

    def test_latch_rtcp_addr_independent(self) -> None:
        session = RTPSession(payload_type=0, codec=get_codec(0), symmetric_rtp=True)
        session._remote_addr = ("10.0.0.1", 4000)
        session._remote_rtcp_addr = ("10.0.0.1", 4001)
        rr = RtcpRrPacket(ssrc=7, reports=[])
        session._handle_rtcp(bytes(rr), ("192.0.2.7", 12346))
        assert session._remote_rtcp_addr == ("192.0.2.7", 12346)
        assert session._remote_addr == ("10.0.0.1", 4000)

    @pytest.mark.asyncio
    async def test_loopback_latching(self) -> None:
        """A starts with a wrong remote port; latching B's source makes A reachable."""
        session_a = await RTPSession.create(
            local_addr=("127.0.0.1", 0),
            remote_addr=("127.0.0.1", 9),  # wrong on purpose
            payload_type=0,
            rtcp_interval=60.0,
            symmetric_rtp=True,
        )
        a_addr = session_a._rtp_transport._transport.get_extra_info("sockname")  # type: ignore[union-attr]
        session_b = await RTPSession.create(
            local_addr=("127.0.0.1", 0),
            remote_addr=(a_addr[0], a_addr[1]),
            payload_type=0,
            rtcp_interval=60.0,
        )
        b_addr = session_b._rtp_transport._transport.get_extra_info("sockname")  # type: ignore[union-attr]

        received = asyncio.Event()
        session_b.on_audio = lambda pcm, ts: received.set()

        # B -> A so A latches B's real source address
        for i in range(2):
            session_b.send_audio(b"\x00" * 160, timestamp=i * 160)
        await asyncio.sleep(0.1)
        assert session_a._remote_addr == (b_addr[0], b_addr[1])

        # A -> B now reaches B without update_remote
        for i in range(6):
            session_a.send_audio(b"\x00" * 160, timestamp=i * 160)
        await asyncio.wait_for(received.wait(), timeout=2.0)

        await session_a.close()
        await session_b.close()


def test_playout_and_paced_require_codec() -> None:
    with pytest.raises(RuntimeError):
        RTPSession(payload_type=0, playout=True)
    with pytest.raises(RuntimeError):
        RTPSession(payload_type=0, paced=True)


@pytest.mark.asyncio
async def test_send_audio_explicit_ts_raises_in_paced_mode() -> None:
    session = await RTPSession.create(
        local_addr=("127.0.0.1", 0),
        remote_addr=("127.0.0.1", 9),
        payload_type=0,
        rtcp_interval=60.0,
        paced=True,
    )
    try:
        with pytest.raises(RuntimeError):
            session.send_audio(b"\x00" * 160, timestamp=0)
        with pytest.raises(RuntimeError):
            session.send_audio_pcm(b"\x00" * 320, timestamp=0)
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_paced_to_playout_loopback() -> None:
    """Paced sender feeds a clocked playout receiver end to end."""
    session_a = await RTPSession.create(
        local_addr=("127.0.0.1", 0),
        remote_addr=("127.0.0.1", 0),
        payload_type=0,
        rtcp_interval=60.0,
        paced=True,
    )
    a_addr = session_a._rtp_transport._transport.get_extra_info("sockname")  # type: ignore[union-attr]
    session_b = await RTPSession.create(
        local_addr=("127.0.0.1", 0),
        remote_addr=a_addr,
        payload_type=0,
        rtcp_interval=60.0,
        playout=True,
    )
    b_addr = session_b._rtp_transport._transport.get_extra_info("sockname")  # type: ignore[union-attr]
    session_a.update_remote(b_addr)

    received: list[tuple[bytes, int]] = []
    session_b.on_audio = lambda pcm, ts: received.append((pcm, ts))

    pcm = struct.pack("<160h", *([2000] * 160))
    for _ in range(10):
        session_a.send_audio_pcm_auto(pcm)

    await asyncio.wait_for(session_a.drain(), timeout=2.0)  # ~200 ms of pacing
    await asyncio.sleep(0.30)  # playout depth + tail concealment + suspension

    # 10 real frames, possibly minus startup and plus tail concealment
    assert len(received) >= 8
    assert all(len(p) == 320 for p, _ in received)  # every tick is 20 ms of PCM
    timestamps = [ts for _, ts in received]
    deltas = [(b - a) & 0xFFFFFFFF for a, b in zip(timestamps, timestamps[1:], strict=False)]
    assert all(d % 160 == 0 for d in deltas)  # delivery stays on the ptime grid

    assert session_a.stats["paced_sent"] == 10
    stats_b = session_b.stats
    assert "playout_delay_ms" in stats_b
    assert "playout_target_ms" in stats_b

    await session_a.close()
    await session_b.close()


def _inject_dtmf(session: RTPSession, seq: int, end: bool) -> None:
    ev = DtmfEvent(event=5, end=end, volume=10, duration=800)
    packet = RtpPacket(payload_type=101, sequence_number=seq, timestamp=480, payload=ev.serialize())
    session._handle_rtp(packet.serialize(), _ADDR)


def test_dtmf_packets_do_not_trigger_concealment() -> None:
    """RFC 4733 packets consume sequence numbers but must not read as loss."""
    session = _plc_session()
    received: list[tuple[bytes, int]] = []
    session.on_audio = lambda pcm, ts: received.append((pcm, ts))
    digits: list[str] = []
    session.on_dtmf = lambda digit, duration: digits.append(digit)

    _inject_pcmu(session, (0, 1, 2))
    for seq in range(3, 10):
        _inject_dtmf(session, seq, end=False)
    for seq in range(10, 13):
        _inject_dtmf(session, seq, end=True)
    _inject_pcmu(session, (13, 14, 15))

    assert digits == ["5"]
    assert session.stats["concealed_frames"] == 0  # no phantom loss from the digit
    assert [ts for _, ts in received] == [0, 160, 320, 13 * 160, 14 * 160]


def test_real_loss_concealed_exactly_next_to_dtmf() -> None:
    """Concealment equals true drops even with a digit in the stream."""
    session = _plc_session()
    received: list[tuple[bytes, int]] = []
    session.on_audio = lambda pcm, ts: received.append((pcm, ts))
    session.on_dtmf = lambda digit, duration: None

    _inject_pcmu(session, (0, 1, 2))
    for seq in range(3, 13):
        _inject_dtmf(session, seq, end=seq >= 10)
    _inject_pcmu(session, (13, 15, 16))  # seq 14 genuinely lost

    assert session.stats["concealed_frames"] == 1  # exactly the dropped packet
    # Timeline stays continuous: concealment PCM fills the seq-14 slot
    assert [ts for _, ts in received] == [0, 160, 320, 13 * 160, 14 * 160, 15 * 160]


@pytest.mark.asyncio
async def test_close_releases_drain_waiters() -> None:
    """Closing with a non-empty paced queue must not deadlock drain()."""
    session = await RTPSession.create(
        local_addr=("127.0.0.1", 0),
        remote_addr=("127.0.0.1", 9),
        payload_type=0,
        rtcp_interval=60.0,
        paced=True,
    )
    for _ in range(50):  # ~1 s of queued audio
        session.send_audio_auto(b"\x00" * 160)
    await session.close()
    await asyncio.wait_for(session.drain(), timeout=0.5)


def test_dtmf_packets_counted_in_stream_stats() -> None:
    """Telephone-events are received stream packets: RR loss stats must not
    report them as lost (they consume sequence numbers)."""
    session = _plc_session()
    session.on_dtmf = lambda digit, duration: None

    _inject_pcmu(session, (0, 1, 2))
    for seq in range(3, 13):
        _inject_dtmf(session, seq, end=seq >= 10)
    _inject_pcmu(session, (13, 15))  # seq 14 genuinely lost

    stats = session._stream_stats
    assert stats is not None
    assert stats.packets_received == 15  # 5 audio + 10 telephone-event
    assert stats.packets_lost == 1  # only the true drop at seq 14


def _inject_cn(session: RTPSession, seq: int, level: int = 60, ts: int = 0) -> None:
    packet = RtpPacket(payload_type=13, sequence_number=seq, timestamp=ts, payload=bytes([level]))
    session._handle_rtp(packet.serialize(), _ADDR)


def test_cn_requires_paced_mode() -> None:
    with pytest.raises(RuntimeError):
        RTPSession(payload_type=0, codec=get_codec(0), cn=True)


def test_cn_packets_not_counted_as_loss() -> None:
    """PT 13 packets consume sequence numbers without reading as loss."""
    session = _plc_session()
    received: list[tuple[bytes, int]] = []
    session.on_audio = lambda pcm, ts: received.append((pcm, ts))
    levels: list[int] = []
    session.on_cn = lambda level: levels.append(level)

    _inject_pcmu(session, (0, 1, 2))
    _inject_cn(session, 3, level=55, ts=480)
    _inject_pcmu(session, (4, 5))

    assert levels == [55]
    assert session.stats["concealed_frames"] == 0
    stats = session._stream_stats
    assert stats is not None
    assert stats.packets_received == 6
    assert stats.packets_lost == 0
    # The CN marker completed the pending ts=320 frame
    assert [ts for _, ts in received] == [0, 160, 320, 4 * 160]


def test_cn_packet_enters_playout_cn_state() -> None:
    session = RTPSession(payload_type=0, codec=get_codec(0), playout=True)
    _inject_pcmu(session, (0, 1))
    _inject_cn(session, 2, level=60, ts=320)

    assert session._playout is not None
    assert session._playout.tick() is not None  # ts=0, real
    assert session._playout.tick() is not None  # ts=160, real
    noise = session._playout.tick()  # ts=320: noise, not concealment
    assert noise is not None
    assert session._playout.cn_frames == 1
    assert session._playout.concealed_frames == 0


@pytest.mark.asyncio
async def test_paced_cn_to_playout_loopback() -> None:
    """Sender silence produces comfort noise at the receiver, end to end."""
    session_a = await RTPSession.create(
        local_addr=("127.0.0.1", 0),
        remote_addr=("127.0.0.1", 0),
        payload_type=0,
        rtcp_interval=60.0,
        paced=True,
        cn=True,
    )
    a_addr = session_a._rtp_transport._transport.get_extra_info("sockname")  # type: ignore[union-attr]
    session_b = await RTPSession.create(
        local_addr=("127.0.0.1", 0),
        remote_addr=a_addr,
        payload_type=0,
        rtcp_interval=60.0,
        playout=True,
    )
    b_addr = session_b._rtp_transport._transport.get_extra_info("sockname")  # type: ignore[union-attr]
    session_a.update_remote(b_addr)

    received: list[tuple[bytes, int]] = []
    session_b.on_audio = lambda pcm, ts: received.append((pcm, ts))

    pcm = struct.pack("<160h", *([4000] * 160))
    for _ in range(5):
        session_a.send_audio_pcm_auto(pcm)
    await asyncio.wait_for(session_a.drain(), timeout=2.0)
    await asyncio.sleep(0.35)  # silence: CN packet + noise generation at B

    assert session_a.stats["cn_sent"] >= 1
    stats_b = session_b.stats
    assert stats_b["cn_frames"] > 0  # receiver generated noise during silence
    assert stats_b["underrun_suspensions"] == 0  # no false dead-sender
    assert len(received) > 5  # real frames plus noise frames

    await session_a.close()
    await session_b.close()
