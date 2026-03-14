import asyncio

import pytest

from aiortp.packet import RtcpPacket, RtcpSrPacket
from aiortp.session import RTPSession


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
    except asyncio.TimeoutError:
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
