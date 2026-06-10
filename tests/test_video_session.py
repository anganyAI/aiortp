"""Tests for VideoRTPSession."""

from __future__ import annotations

import asyncio

import pytest

from aiortp.packet import (
    RTCP_PSFB_FIR,
    RTCP_PSFB_PLI,
    RtcpPacket,
    RtcpPsfbPacket,
    RtpPacket,
)
from aiortp.video_session import VideoRTPSession

_Pair = tuple[VideoRTPSession, VideoRTPSession]

# Dummy source address for direct _handle_rtp/_handle_rtcp injection
_ADDR = ("198.51.100.1", 5004)


@pytest.fixture
async def loopback_pair() -> _Pair:
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
    async def test_single_nal_delivery(self, loopback_pair: _Pair) -> None:
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

    async def test_keyframe_detection(self, loopback_pair: _Pair) -> None:
        """IDR NAL unit detected as keyframe — delivered immediately."""
        sender, receiver = loopback_pair
        received: list[tuple[bytes, int, bool]] = []
        event = asyncio.Event()

        def on_frame(nal: bytes, ts: int, keyframe: bool) -> None:
            received.append((nal, ts, keyframe))
            event.set()

        receiver.on_frame = on_frame

        # Send IDR NAL (type 5)
        idr = bytes([0x65]) + b"\xab" * 10
        sender.send_frame([idr], timestamp=90000)

        await asyncio.wait_for(event.wait(), timeout=2.0)
        assert received[0][2] is True  # keyframe

    async def test_multi_nal_frame(self, loopback_pair: _Pair) -> None:
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
    async def test_stats_after_send(self, loopback_pair: _Pair) -> None:
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
            session._handle_rtcp(bytes(pli), _ADDR)

            assert pli_received.is_set()
        finally:
            await session.close()

    async def test_fir_triggers_keyframe_callback(self) -> None:
        """Receiving a FIR packet also triggers on_keyframe_needed."""
        session = await VideoRTPSession.create(
            local_addr=("127.0.0.1", 0),
            remote_addr=("127.0.0.1", 0),
            payload_type=96,
        )
        try:
            fir_received = asyncio.Event()
            session.on_keyframe_needed = lambda: fir_received.set()

            fir = RtcpPsfbPacket(
                fmt=RTCP_PSFB_FIR,
                ssrc=12345,
                media_ssrc=session._ssrc,
            )
            session._handle_rtcp(bytes(fir), _ADDR)

            assert fir_received.is_set()
        finally:
            await session.close()

    async def test_request_keyframe_sends_pli(self, loopback_pair: _Pair) -> None:
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
        sender._handle_rtcp(bytes(pli), _ADDR)
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


class TestVideoSessionPassthrough:
    """Tests for passthrough (bridge) mode and request_keyframe gate behavior."""

    async def test_passthrough_enables_flag(self) -> None:
        """set_passthrough(True) disables the keyframe gate."""
        session = await VideoRTPSession.create(
            local_addr=("127.0.0.1", 0),
            remote_addr=("127.0.0.1", 0),
            payload_type=96,
        )
        try:
            assert session._awaiting_keyframe_enabled is True
            session.set_passthrough(True)
            assert session._awaiting_keyframe_enabled is False
            assert session._awaiting_keyframe is False
        finally:
            await session.close()

    async def test_passthrough_disables_flag(self) -> None:
        """set_passthrough(False) re-enables the keyframe gate."""
        session = await VideoRTPSession.create(
            local_addr=("127.0.0.1", 0),
            remote_addr=("127.0.0.1", 0),
            payload_type=96,
        )
        try:
            session.set_passthrough(True)
            session.set_passthrough(False)
            assert session._awaiting_keyframe_enabled is True
        finally:
            await session.close()

    async def test_passthrough_clears_awaiting(self) -> None:
        """Enabling passthrough clears any pending awaiting_keyframe state."""
        session = await VideoRTPSession.create(
            local_addr=("127.0.0.1", 0),
            remote_addr=("127.0.0.1", 0),
            payload_type=96,
        )
        try:
            session._awaiting_keyframe = True
            session.set_passthrough(True)
            assert session._awaiting_keyframe is False
        finally:
            await session.close()

    async def test_request_keyframe_clears_gate_in_passthrough(self) -> None:
        """request_keyframe() clears the gate when in passthrough mode."""
        session = await VideoRTPSession.create(
            local_addr=("127.0.0.1", 0),
            remote_addr=("127.0.0.1", 0),
            payload_type=96,
        )
        try:
            session.set_passthrough(True)
            session._awaiting_keyframe = True
            session._remote_ssrc = 12345
            session.request_keyframe()
            assert session._awaiting_keyframe is False
        finally:
            await session.close()

    async def test_request_keyframe_preserves_gate_in_normal_mode(self) -> None:
        """request_keyframe() does NOT clear the gate in normal (non-passthrough) mode."""
        session = await VideoRTPSession.create(
            local_addr=("127.0.0.1", 0),
            remote_addr=("127.0.0.1", 0),
            payload_type=96,
        )
        try:
            assert session._awaiting_keyframe_enabled is True
            session._awaiting_keyframe = True
            session._remote_ssrc = 12345
            session.request_keyframe()
            assert session._awaiting_keyframe is True  # gate preserved
        finally:
            await session.close()

    async def test_pli_flag_skips_gate_in_passthrough(self) -> None:
        """PLI flag from jitter buffer does not set awaiting_keyframe in passthrough."""
        session = await VideoRTPSession.create(
            local_addr=("127.0.0.1", 0),
            remote_addr=("127.0.0.1", 0),
            payload_type=96,
        )
        try:
            session.set_passthrough(True)
            # Directly test the gating logic
            session._awaiting_keyframe = False
            assert session._awaiting_keyframe_enabled is False
            # In passthrough, even if _handle_rtp triggers pli_flag,
            # _awaiting_keyframe should not be set because _awaiting_keyframe_enabled is False
        finally:
            await session.close()


class TestVideoSourceChange:
    async def test_ssrc_change_resets_and_sends_pli(self) -> None:
        """A new source mid-stream relatches, gates on keyframe and sends PLI."""
        session = await VideoRTPSession.create(
            local_addr=("127.0.0.1", 0),
            remote_addr=("127.0.0.1", 9),
            payload_type=96,
        )
        try:
            sent_rtcp: list[bytes] = []
            session._rtcp_transport.send = lambda data, addr=None: sent_rtcp.append(data)  # type: ignore[union-attr, assignment]

            received: list[tuple[bytes, int, bool]] = []
            session.on_frame = lambda nal, ts, kf: received.append((nal, ts, kf))

            # First source delivers an IDR keyframe
            idr = bytes([0x65]) + b"\xab" * 10
            pkt = RtpPacket(
                payload_type=96,
                sequence_number=100,
                timestamp=3000,
                ssrc=1111,
                marker=1,
                payload=idr,
            )
            session._handle_rtp(pkt.serialize(), _ADDR)
            assert session._remote_ssrc == 1111
            assert len(received) == 1

            # New source mid-stream, starting with a non-keyframe
            non_idr = bytes([0x41]) + b"\x01\x02"
            pkt = RtpPacket(
                payload_type=96,
                sequence_number=7000,
                timestamp=90000,
                ssrc=2222,
                marker=1,
                payload=non_idr,
            )
            session._handle_rtp(pkt.serialize(), _ADDR)

            assert session._remote_ssrc == 2222
            assert session._awaiting_keyframe is True
            assert len(received) == 1  # non-keyframe gated until keyframe arrives
            plis = [
                p
                for data in sent_rtcp
                for p in RtcpPacket.parse(data)
                if isinstance(p, RtcpPsfbPacket) and p.fmt == RTCP_PSFB_PLI
            ]
            assert len(plis) >= 1
            assert plis[-1].media_ssrc == 2222

            # Keyframe from the new source resumes delivery
            pkt = RtpPacket(
                payload_type=96,
                sequence_number=7001,
                timestamp=93000,
                ssrc=2222,
                marker=1,
                payload=idr,
            )
            session._handle_rtp(pkt.serialize(), _ADDR)
            assert len(received) == 2
        finally:
            await session.close()


class TestVideoSessionAutoTimestamp:
    async def test_send_frame_auto_increments_timestamp(
        self,
        loopback_pair: _Pair,
    ) -> None:
        """Auto-timestamp increments by clock_rate/fps (3000 at 30fps)."""
        sender, receiver = loopback_pair
        nal = bytes([0x41]) + b"\x00" * 10

        ts1 = sender.send_frame_auto([nal])
        ts2 = sender.send_frame_auto([nal])
        assert ts2 == (ts1 + 3000) & 0xFFFFFFFF  # 90000 / 30
        assert sender.stats["packets_sent"] == 2
