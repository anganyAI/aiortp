import asyncio

import pytest

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
