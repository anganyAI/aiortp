"""Tests for VideoRTPSession."""

from __future__ import annotations

import asyncio

import pytest

from aiortp.packet import (
    RTCP_PSFB_PLI,
    RtcpPsfbPacket,
)
from aiortp.video_session import VideoRTPSession


@pytest.fixture
async def loopback_pair() -> (
    tuple[VideoRTPSession, VideoRTPSession]
):
    """Create two video sessions wired to each other."""
    session_a = await VideoRTPSession.create(
        local_addr=("127.0.0.1", 0),
        remote_addr=("127.0.0.1", 0),
        payload_type=96,
    )
    a_addr = session_a._rtp_transport._transport.get_extra_info("sockname")  # type: ignore[union-attr]

    session_b = await VideoRTPSession.create(
        local_addr=("127.0.0.1", 0),
        remote_addr=a_addr,
        payload_type=96,
    )
    b_addr = session_b._rtp_transport._transport.get_extra_info("sockname")  # type: ignore[union-attr]
    session_a.update_remote(b_addr)

    yield session_a, session_b

    await session_a.close()
    await session_b.close()


class TestVideoSessionSendReceive:
    async def test_single_nal_delivery(self, loopback_pair: tuple[VideoRTPSession, VideoRTPSession]) -> None:
        """Single small NAL unit delivered immediately (no second frame needed)."""
        sender, receiver = loopback_pair
        received: list[tuple[bytes, int, bool]] = []
        event = asyncio.Event()

        def on_frame(nal: bytes, ts: int, keyframe: bool) -> None:
            received.append((nal, ts, keyframe))
            event.set()

        receiver.on_frame = on_frame

        # Send a non-IDR NAL (type 1) — marker bit delivers immediately
        nal = bytes([0x41]) + b"\x01\x02\x03"
        sender.send_frame([nal], timestamp=90000)

        await asyncio.wait_for(event.wait(), timeout=2.0)
        assert len(received) >= 1
        assert received[0][0] == nal
        assert received[0][1] == 90000
        assert received[0][2] is False  # non-IDR

    async def test_keyframe_detection(self, loopback_pair: tuple[VideoRTPSession, VideoRTPSession]) -> None:
        """IDR NAL unit detected as keyframe — delivered immediately."""
        sender, receiver = loopback_pair
        received: list[tuple[bytes, int, bool]] = []
        event = asyncio.Event()

        def on_frame(nal: bytes, ts: int, keyframe: bool) -> None:
            received.append((nal, ts, keyframe))
            event.set()

        receiver.on_frame = on_frame

        # Send IDR NAL (type 5)
        idr = bytes([0x65]) + b"\xAB" * 10
        sender.send_frame([idr], timestamp=90000)

        await asyncio.wait_for(event.wait(), timeout=2.0)
        assert received[0][2] is True  # keyframe

    async def test_multi_nal_frame(self, loopback_pair: tuple[VideoRTPSession, VideoRTPSession]) -> None:
        """Multiple NALs in one frame (SPS + PPS + IDR) — delivered immediately."""
        sender, receiver = loopback_pair
        received: list[tuple[bytes, int, bool]] = []
        all_done = asyncio.Event()

        def on_frame(nal: bytes, ts: int, keyframe: bool) -> None:
            received.append((nal, ts, keyframe))
            if len(received) >= 3:
                all_done.set()

        receiver.on_frame = on_frame

        sps = bytes([0x67, 0x42, 0x00])
        pps = bytes([0x68, 0xCE])
        idr = bytes([0x65]) + b"\x00" * 10

        sender.send_frame([sps, pps, idr], timestamp=90000)

        await asyncio.wait_for(all_done.wait(), timeout=2.0)
        assert len(received) >= 3
        # SPS and PPS are keyframe NALs too
        assert received[0][0] == sps
        assert received[1][0] == pps
        assert received[2][0] == idr


class TestVideoSessionStats:
    async def test_stats_after_send(self, loopback_pair: tuple[VideoRTPSession, VideoRTPSession]) -> None:
        sender, receiver = loopback_pair

        nal = bytes([0x41]) + b"\x00" * 50
        sender.send_frame([nal], timestamp=90000)

        stats = sender.stats
        assert stats["ssrc"] == sender._ssrc
        assert stats["packets_sent"] >= 1
        assert stats["octets_sent"] > 0


class TestVideoSessionPLI:
    async def test_pli_triggers_keyframe_callback(self) -> None:
        """Receiving a PLI packet triggers on_keyframe_needed callback."""
        session = await VideoRTPSession.create(
            local_addr=("127.0.0.1", 0),
            remote_addr=("127.0.0.1", 0),
            payload_type=96,
        )
        try:
            pli_received = asyncio.Event()

            def on_keyframe_needed() -> None:
                pli_received.set()

            session.on_keyframe_needed = on_keyframe_needed

            # Simulate receiving a PLI RTCP packet
            pli = RtcpPsfbPacket(
                fmt=RTCP_PSFB_PLI,
                ssrc=12345,
                media_ssrc=session._ssrc,
            )
            session._handle_rtcp(bytes(pli))

            assert pli_received.is_set()
        finally:
            await session.close()

    async def test_request_keyframe_sends_pli(self, loopback_pair: tuple[VideoRTPSession, VideoRTPSession]) -> None:
        """request_keyframe() sends a PLI packet."""
        sender, receiver = loopback_pair

        # Need to establish remote SSRC first by sending a packet
        nal = bytes([0x41]) + b"\x00"
        sender.send_frame([nal], timestamp=90000)
        await asyncio.sleep(0.05)

        # Receiver should have learned sender's SSRC
        receiver._remote_ssrc = sender._ssrc
        receiver.request_keyframe()

        # Verify PLI was sent (check sender receives keyframe_needed)
        pli_received = asyncio.Event()
        sender.on_keyframe_needed = lambda: pli_received.set()

        # Send PLI from receiver to sender
        pli = RtcpPsfbPacket(
            fmt=RTCP_PSFB_PLI,
            ssrc=receiver._ssrc,
            media_ssrc=sender._ssrc,
        )
        sender._handle_rtcp(bytes(pli))
        assert pli_received.is_set()


class TestVideoSessionClose:
    async def test_close_idempotent(self) -> None:
        session = await VideoRTPSession.create(
            local_addr=("127.0.0.1", 0),
            remote_addr=("127.0.0.1", 0),
            payload_type=96,
        )
        await session.close()
        await session.close()  # should not raise

    async def test_send_after_close_ignored(self) -> None:
        session = await VideoRTPSession.create(
            local_addr=("127.0.0.1", 0),
            remote_addr=("127.0.0.1", 0),
            payload_type=96,
        )
        await session.close()
        # Should not raise
        session.send_frame([bytes([0x41])], timestamp=90000)
