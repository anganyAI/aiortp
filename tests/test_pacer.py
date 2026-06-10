"""Tests for PacedSender and MediaClock."""

import asyncio

import pytest

from aiortp.clock import MediaClock
from aiortp.pacer import PacedSender
from aiortp.packet import RtpPacket
from aiortp.sender import RtpSender
from aiortp.transport import RtpTransport


def _sender_with_recorder() -> tuple[RtpSender, list[bytes]]:
    sent: list[bytes] = []
    transport = RtpTransport(on_rtp=lambda d, a: None, on_rtcp=lambda d, a: None)
    transport.send = lambda data, addr=None: sent.append(data)  # type: ignore[assignment]
    sender = RtpSender(transport=transport, payload_type=0, ssrc=1000)
    sender.timestamp_increment = 160
    return sender, sent


@pytest.mark.asyncio
async def test_ticks_transmit_one_frame_each() -> None:
    sender, sent = _sender_with_recorder()
    pacer = PacedSender(sender, get_addr=lambda: ("127.0.0.1", 9))

    for i in range(3):
        pacer.enqueue(bytes([i]) * 160)
    for _ in range(3):
        pacer.tick()

    assert len(sent) == 3
    assert pacer.paced_sent == 3
    timestamps = [RtpPacket.parse(d).timestamp for d in sent]
    assert timestamps[1] - timestamps[0] == 160
    assert timestamps[2] - timestamps[1] == 160


@pytest.mark.asyncio
async def test_empty_tick_advances_timestamp() -> None:
    """Silence appears as a timestamp jump, not stale packets."""
    sender, sent = _sender_with_recorder()
    pacer = PacedSender(sender, get_addr=lambda: None)

    pacer.enqueue(b"\x00" * 160)
    pacer.tick()
    pacer.tick()  # empty
    pacer.tick()  # empty
    pacer.enqueue(b"\x01" * 160)
    pacer.tick()

    assert len(sent) == 2
    assert pacer.empty_ticks == 2
    ts0, ts1 = (RtpPacket.parse(d).timestamp for d in sent)
    assert (ts1 - ts0) & 0xFFFFFFFF == 3 * 160


@pytest.mark.asyncio
async def test_drain_waits_for_queue() -> None:
    sender, sent = _sender_with_recorder()
    pacer = PacedSender(sender, get_addr=lambda: None)
    clock = MediaClock(0.005, pacer.tick)
    clock.start()
    try:
        for i in range(4):
            pacer.enqueue(bytes([i]) * 160)
        await asyncio.wait_for(pacer.drain(), timeout=2.0)
        assert pacer.queue_depth == 0
        assert len(sent) == 4
    finally:
        await clock.stop()


@pytest.mark.asyncio
async def test_drain_returns_immediately_when_empty() -> None:
    sender, _ = _sender_with_recorder()
    pacer = PacedSender(sender, get_addr=lambda: None)
    await asyncio.wait_for(pacer.drain(), timeout=0.5)


def test_stats_shape() -> None:
    sender, _ = _sender_with_recorder()
    pacer = PacedSender(sender, get_addr=lambda: None)
    assert pacer.stats() == {"queue_depth": 0, "paced_sent": 0, "empty_ticks": 0}
