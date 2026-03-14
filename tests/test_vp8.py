"""Tests for VP8 RTP depacketization and packetization (RFC 7741)."""

from __future__ import annotations

from aiortp.vp8 import (
    _I_BIT,
    _L_BIT,
    _M_BIT,
    _S_BIT,
    _T_BIT,
    _X_BIT,
    VP8Depacketizer,
    VP8Packetizer,
    _parse_descriptor_offset,
    is_keyframe_vp8,
)


def _make_vp8_payload(
    *,
    start: bool = False,
    keyframe: bool = False,
    extended: bool = False,
    pid: int | None = None,
    tl0picidx: bool = False,
    tid: bool = False,
    data: bytes = b"\xaa\xbb",
) -> bytes:
    """Build a minimal VP8 RTP payload with the given flags."""
    first = 0
    if start:
        first |= _S_BIT
    if pid is not None or tl0picidx or tid:
        extended = True
    if extended:
        first |= _X_BIT

    buf = bytearray([first])

    if extended:
        ext = 0
        if pid is not None:
            ext |= _I_BIT
        if tl0picidx:
            ext |= _L_BIT
        if tid:
            ext |= _T_BIT
        buf.append(ext)

    if pid is not None:
        if pid > 127:
            buf.append(_M_BIT | ((pid >> 8) & 0x7F))
            buf.append(pid & 0xFF)
        else:
            buf.append(pid & 0x7F)

    if tl0picidx:
        buf.append(0x00)

    if tid:
        buf.append(0x00)  # TID=0, Y=0, KEYIDX=0

    # VP8 frame tag: bit 0 = 0 for keyframe, 1 for inter-frame
    if start:
        frame_tag = 0x00 if keyframe else 0x01
        buf.append(frame_tag)
        buf.extend(data)
    else:
        buf.extend(data)

    return bytes(buf)


# ── is_keyframe_vp8 ──────────────────────────────────────────────


class TestIsKeyframeVP8:
    def test_keyframe_start(self) -> None:
        payload = _make_vp8_payload(start=True, keyframe=True)
        assert is_keyframe_vp8(payload) is True

    def test_inter_frame_not_keyframe(self) -> None:
        payload = _make_vp8_payload(start=True, keyframe=False)
        assert is_keyframe_vp8(payload) is False

    def test_continuation_not_keyframe(self) -> None:
        # S=0 — not a start, so can't be keyframe
        payload = _make_vp8_payload(start=False, keyframe=True)
        assert is_keyframe_vp8(payload) is False

    def test_empty(self) -> None:
        assert is_keyframe_vp8(b"") is False


# ── _parse_descriptor_offset ─────────────────────────────────────


class TestParseDescriptorOffset:
    def test_minimal(self) -> None:
        """No extensions — offset is 1 (mandatory byte only)."""
        payload = bytes([_S_BIT]) + b"\xff"
        assert _parse_descriptor_offset(payload) == 1

    def test_with_extension_no_fields(self) -> None:
        """X=1 but no optional fields set."""
        payload = bytes([_X_BIT, 0x00]) + b"\xff"
        assert _parse_descriptor_offset(payload) == 2

    def test_with_7bit_pid(self) -> None:
        payload = _make_vp8_payload(start=True, pid=42, data=b"\xff")
        # 1 (mandatory) + 1 (ext) + 1 (7-bit PID) + 1 (frame tag)
        assert _parse_descriptor_offset(payload) == 3

    def test_with_15bit_pid(self) -> None:
        payload = _make_vp8_payload(start=True, pid=300, data=b"\xff")
        # 1 (mandatory) + 1 (ext) + 2 (15-bit PID) + frame tag after
        assert _parse_descriptor_offset(payload) == 4

    def test_with_tl0picidx(self) -> None:
        payload = _make_vp8_payload(start=True, tl0picidx=True, data=b"\xff")
        # 1 (mandatory) + 1 (ext) + 1 (TL0PICIDX)
        assert _parse_descriptor_offset(payload) == 3

    def test_with_tid(self) -> None:
        payload = _make_vp8_payload(start=True, tid=True, data=b"\xff")
        # 1 (mandatory) + 1 (ext) + 1 (TID/Y/KEYIDX)
        assert _parse_descriptor_offset(payload) == 3

    def test_with_all_fields(self) -> None:
        payload = _make_vp8_payload(
            start=True,
            pid=42,
            tl0picidx=True,
            tid=True,
            data=b"\xff",
        )
        # 1 (mandatory) + 1 (ext) + 1 (PID) + 1 (TL0PICIDX) + 1 (TID)
        assert _parse_descriptor_offset(payload) == 5


# ── VP8Depacketizer ──────────────────────────────────────────────


class TestVP8DepacketizerSinglePacket:
    """Frame that fits in a single RTP packet (S=1, marker=True)."""

    def test_single_packet_frame(self) -> None:
        depkt = VP8Depacketizer()
        payload = _make_vp8_payload(
            start=True,
            keyframe=True,
            data=b"\x01\x02\x03",
        )
        result = depkt.feed(payload, marker=True)
        assert len(result) == 1
        frame_data, is_kf = result[0]
        assert is_kf is True
        assert len(frame_data) > 0

    def test_inter_frame(self) -> None:
        depkt = VP8Depacketizer()
        payload = _make_vp8_payload(
            start=True,
            keyframe=False,
            data=b"\xaa",
        )
        result = depkt.feed(payload, marker=True)
        assert len(result) == 1
        _, is_kf = result[0]
        assert is_kf is False


class TestVP8DepacketizerFragmented:
    """Frame split across multiple RTP packets."""

    def test_two_fragments(self) -> None:
        depkt = VP8Depacketizer()

        frag1 = _make_vp8_payload(start=True, data=b"\x01\x02")
        frag2 = _make_vp8_payload(start=False, data=b"\x03\x04")

        result1 = depkt.feed(frag1, marker=False)
        assert result1 == []

        result2 = depkt.feed(frag2, marker=True)
        assert len(result2) == 1

    def test_three_fragments(self) -> None:
        depkt = VP8Depacketizer()

        frag1 = _make_vp8_payload(start=True, keyframe=True, data=b"\x01")
        frag2 = _make_vp8_payload(start=False, data=b"\x02")
        frag3 = _make_vp8_payload(start=False, data=b"\x03")

        assert depkt.feed(frag1, marker=False) == []
        assert depkt.feed(frag2, marker=False) == []
        result = depkt.feed(frag3, marker=True)
        assert len(result) == 1
        _, is_kf = result[0]
        assert is_kf is True

    def test_middle_without_start_discarded(self) -> None:
        depkt = VP8Depacketizer()
        frag = _make_vp8_payload(start=False, data=b"\xff")
        assert depkt.feed(frag, marker=True) == []

    def test_reset_discards_partial(self) -> None:
        depkt = VP8Depacketizer()
        frag1 = _make_vp8_payload(start=True, data=b"\x01")
        depkt.feed(frag1, marker=False)

        depkt.reset()

        frag2 = _make_vp8_payload(start=False, data=b"\x02")
        assert depkt.feed(frag2, marker=True) == []

    def test_new_start_replaces_partial(self) -> None:
        depkt = VP8Depacketizer()
        frag1 = _make_vp8_payload(start=True, data=b"\x01")
        depkt.feed(frag1, marker=False)

        frag2 = _make_vp8_payload(start=True, data=b"\x02")
        result = depkt.feed(frag2, marker=True)
        assert len(result) == 1


class TestVP8DepacketizerWithPID:
    """Payloads with Picture ID present."""

    def test_7bit_pid_stripped(self) -> None:
        depkt = VP8Depacketizer()
        payload = _make_vp8_payload(
            start=True,
            pid=42,
            data=b"\xde\xad",
        )
        result = depkt.feed(payload, marker=True)
        assert len(result) == 1

    def test_15bit_pid_stripped(self) -> None:
        depkt = VP8Depacketizer()
        payload = _make_vp8_payload(
            start=True,
            pid=300,
            data=b"\xbe\xef",
        )
        result = depkt.feed(payload, marker=True)
        assert len(result) == 1


# ── VP8Packetizer ────────────────────────────────────────────────


class TestVP8PacketizerSinglePacket:
    def test_small_frame(self) -> None:
        pkt = VP8Packetizer()
        result = pkt.packetize(b"\x00\x01\x02\x03", keyframe=True)
        assert len(result) == 1
        payload, is_last = result[0]
        assert is_last is True
        assert payload[0] & _S_BIT  # S=1
        assert payload[1:] == b"\x00\x01\x02\x03"

    def test_empty_frame(self) -> None:
        pkt = VP8Packetizer()
        assert pkt.packetize(b"") == []


class TestVP8PacketizerFragmented:
    def test_large_frame_fragmented(self) -> None:
        pkt = VP8Packetizer()
        frame = bytes(range(256)) * 10  # 2560 bytes
        result = pkt.packetize(frame, mtu=1000, keyframe=False)
        assert len(result) >= 3

        # First has S=1
        assert result[0][0][0] & _S_BIT
        assert result[0][1] is False

        # Middle has S=0
        for payload, is_last in result[1:-1]:
            assert not (payload[0] & _S_BIT)
            assert is_last is False

        # Last is_last=True
        assert not (result[-1][0][0] & _S_BIT)
        assert result[-1][1] is True

    def test_each_fragment_within_mtu(self) -> None:
        pkt = VP8Packetizer()
        mtu = 50
        frame = b"\xff" * 300
        result = pkt.packetize(frame, mtu=mtu)
        for payload, _ in result:
            assert len(payload) <= mtu


# ── Roundtrip ────────────────────────────────────────────────────


class TestVP8Roundtrip:
    def test_small_frame_roundtrip(self) -> None:
        pkt = VP8Packetizer()
        depkt = VP8Depacketizer()
        original = b"\x00\x01\x02\x03\x04\x05"

        packets = pkt.packetize(original, keyframe=True)
        frames: list[tuple[bytes, bool]] = []
        for payload, is_last in packets:
            frames.extend(depkt.feed(payload, marker=is_last))

        assert len(frames) == 1
        assert frames[0][0] == original

    def test_large_frame_roundtrip(self) -> None:
        pkt = VP8Packetizer()
        depkt = VP8Depacketizer()
        original = bytes(range(256)) * 20  # 5120 bytes

        packets = pkt.packetize(original, mtu=1000, keyframe=False)
        frames: list[tuple[bytes, bool]] = []
        for payload, is_last in packets:
            frames.extend(depkt.feed(payload, marker=is_last))

        assert len(frames) == 1
        assert frames[0][0] == original

    def test_multiple_frames_roundtrip(self) -> None:
        pkt = VP8Packetizer()
        depkt = VP8Depacketizer()

        frame1 = b"\x00" * 100
        frame2 = b"\x01" * 200

        all_frames: list[tuple[bytes, bool]] = []
        for payload, is_last in pkt.packetize(frame1, keyframe=True):
            all_frames.extend(depkt.feed(payload, marker=is_last))
        for payload, is_last in pkt.packetize(frame2, keyframe=False):
            all_frames.extend(depkt.feed(payload, marker=is_last))

        assert len(all_frames) == 2
        assert all_frames[0][0] == frame1
        assert all_frames[1][0] == frame2
