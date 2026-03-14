"""Tests for VP9 RTP depacketization and packetization (RFC 9628)."""

from __future__ import annotations

from aiortp.vp9 import (
    _B_BIT,
    _E_BIT,
    _F_BIT,
    _I_BIT,
    _L_BIT,
    _M_BIT,
    _P_BIT,
    VP9Depacketizer,
    VP9Packetizer,
    _parse_descriptor_offset,
    is_keyframe_vp9,
)


def _make_vp9_payload(
    *,
    start: bool = False,
    end: bool = False,
    keyframe: bool = False,
    pid: int | None = None,
    layer: bool = False,
    flexible: bool = False,
    data: bytes = b"\xaa\xbb",
) -> bytes:
    """Build a minimal VP9 RTP payload with the given flags."""
    first = 0
    if not keyframe:
        first |= _P_BIT
    if start:
        first |= _B_BIT
    if end:
        first |= _E_BIT
    if pid is not None:
        first |= _I_BIT
    if layer:
        first |= _L_BIT
    if flexible:
        first |= _F_BIT

    buf = bytearray([first])

    if pid is not None:
        if pid > 127:
            buf.append(_M_BIT | ((pid >> 8) & 0x7F))
            buf.append(pid & 0xFF)
        else:
            buf.append(pid & 0x7F)

    if layer:
        buf.append(0x00)  # TID=0, U=0, SID=0, D=0
        if not flexible:
            buf.append(0x00)  # TL0PICIDX (non-flexible mode)

    buf.extend(data)
    return bytes(buf)


# ── is_keyframe_vp9 ──────────────────────────────────────────────


class TestIsKeyframeVP9:
    def test_keyframe_start(self) -> None:
        payload = _make_vp9_payload(start=True, keyframe=True)
        assert is_keyframe_vp9(payload) is True

    def test_inter_frame_not_keyframe(self) -> None:
        payload = _make_vp9_payload(start=True, keyframe=False)
        assert is_keyframe_vp9(payload) is False

    def test_keyframe_not_start(self) -> None:
        # P=0 but B=0 — continuation of keyframe, not a start
        payload = _make_vp9_payload(start=False, keyframe=True)
        assert is_keyframe_vp9(payload) is False

    def test_empty(self) -> None:
        assert is_keyframe_vp9(b"") is False


# ── _parse_descriptor_offset ─────────────────────────────────────


class TestParseDescriptorOffset:
    def test_minimal(self) -> None:
        """No optional fields — offset is 1 (mandatory byte only)."""
        payload = bytes([_B_BIT | _E_BIT]) + b"\xff"
        assert _parse_descriptor_offset(payload) == 1

    def test_with_7bit_pid(self) -> None:
        payload = _make_vp9_payload(pid=42, data=b"\xff")
        # 1 (mandatory) + 1 (7-bit PID)
        assert _parse_descriptor_offset(payload) == 2

    def test_with_15bit_pid(self) -> None:
        payload = _make_vp9_payload(pid=300, data=b"\xff")
        # 1 (mandatory) + 2 (15-bit PID)
        assert _parse_descriptor_offset(payload) == 3

    def test_with_layer_indices_non_flexible(self) -> None:
        """L=1, F=0: TID/U/SID/D + TL0PICIDX = 2 bytes."""
        payload = _make_vp9_payload(layer=True, flexible=False, data=b"\xff")
        # 1 (mandatory) + 1 (TID/U/SID/D) + 1 (TL0PICIDX)
        assert _parse_descriptor_offset(payload) == 3

    def test_with_layer_indices_flexible(self) -> None:
        """L=1, F=1: TID/U/SID/D only = 1 byte (no TL0PICIDX).

        Uses keyframe=True (P=0) to avoid reference index parsing
        that occurs when both F=1 and P=1.
        """
        payload = _make_vp9_payload(
            layer=True,
            flexible=True,
            keyframe=True,
            data=b"\xff",
        )
        # 1 (mandatory) + 1 (TID/U/SID/D)
        assert _parse_descriptor_offset(payload) == 2

    def test_with_pid_and_layer_non_flexible(self) -> None:
        payload = _make_vp9_payload(pid=10, layer=True, flexible=False, data=b"\xff")
        # 1 (mandatory) + 1 (7-bit PID) + 1 (TID/U/SID/D) + 1 (TL0PICIDX)
        assert _parse_descriptor_offset(payload) == 4

    def test_with_pid_and_layer_flexible(self) -> None:
        """Uses keyframe=True (P=0) to avoid reference index parsing."""
        payload = _make_vp9_payload(
            pid=10,
            layer=True,
            flexible=True,
            keyframe=True,
            data=b"\xff",
        )
        # 1 (mandatory) + 1 (7-bit PID) + 1 (TID/U/SID/D)
        assert _parse_descriptor_offset(payload) == 3


# ── VP9Depacketizer ──────────────────────────────────────────────


class TestVP9DepacketizerSinglePacket:
    """Frame that fits in a single RTP packet (B=1, E=1)."""

    def test_single_packet_frame(self) -> None:
        depkt = VP9Depacketizer()
        payload = _make_vp9_payload(
            start=True,
            end=True,
            keyframe=True,
            data=b"\x01\x02\x03",
        )
        result = depkt.feed(payload, marker=True)
        assert len(result) == 1
        frame_data, is_kf = result[0]
        assert frame_data == b"\x01\x02\x03"
        assert is_kf is True

    def test_inter_frame(self) -> None:
        depkt = VP9Depacketizer()
        payload = _make_vp9_payload(
            start=True,
            end=True,
            keyframe=False,
            data=b"\xaa",
        )
        result = depkt.feed(payload, marker=True)
        assert len(result) == 1
        _, is_kf = result[0]
        assert is_kf is False


class TestVP9DepacketizerFragmented:
    """Frame split across multiple RTP packets."""

    def test_two_fragments(self) -> None:
        depkt = VP9Depacketizer()

        frag1 = _make_vp9_payload(start=True, end=False, data=b"\x01\x02")
        frag2 = _make_vp9_payload(start=False, end=True, data=b"\x03\x04")

        result1 = depkt.feed(frag1, marker=False)
        assert result1 == []

        result2 = depkt.feed(frag2, marker=True)
        assert len(result2) == 1
        assert result2[0][0] == b"\x01\x02\x03\x04"

    def test_three_fragments(self) -> None:
        depkt = VP9Depacketizer()

        frag1 = _make_vp9_payload(
            start=True,
            end=False,
            keyframe=True,
            data=b"\x01",
        )
        frag2 = _make_vp9_payload(start=False, end=False, data=b"\x02")
        frag3 = _make_vp9_payload(start=False, end=True, data=b"\x03")

        assert depkt.feed(frag1, marker=False) == []
        assert depkt.feed(frag2, marker=False) == []
        result = depkt.feed(frag3, marker=True)
        assert len(result) == 1
        frame_data, is_kf = result[0]
        assert frame_data == b"\x01\x02\x03"
        assert is_kf is True

    def test_middle_without_start_discarded(self) -> None:
        depkt = VP9Depacketizer()
        # Feed a middle fragment without a preceding start
        frag = _make_vp9_payload(start=False, end=True, data=b"\xff")
        assert depkt.feed(frag, marker=True) == []

    def test_reset_discards_partial(self) -> None:
        depkt = VP9Depacketizer()
        frag1 = _make_vp9_payload(start=True, end=False, data=b"\x01")
        depkt.feed(frag1, marker=False)

        depkt.reset()

        # End fragment after reset should be discarded
        frag2 = _make_vp9_payload(start=False, end=True, data=b"\x02")
        assert depkt.feed(frag2, marker=True) == []

    def test_new_start_replaces_partial(self) -> None:
        depkt = VP9Depacketizer()
        frag1 = _make_vp9_payload(start=True, end=False, data=b"\x01")
        depkt.feed(frag1, marker=False)

        # New start replaces the partial frame
        frag2 = _make_vp9_payload(start=True, end=True, data=b"\x02")
        result = depkt.feed(frag2, marker=True)
        assert len(result) == 1
        assert result[0][0] == b"\x02"


class TestVP9DepacketizerWithPID:
    """Payloads with Picture ID present."""

    def test_7bit_pid_stripped(self) -> None:
        depkt = VP9Depacketizer()
        payload = _make_vp9_payload(
            start=True,
            end=True,
            pid=42,
            data=b"\xde\xad",
        )
        result = depkt.feed(payload, marker=True)
        assert len(result) == 1
        assert result[0][0] == b"\xde\xad"

    def test_15bit_pid_stripped(self) -> None:
        depkt = VP9Depacketizer()
        payload = _make_vp9_payload(
            start=True,
            end=True,
            pid=300,
            data=b"\xbe\xef",
        )
        result = depkt.feed(payload, marker=True)
        assert len(result) == 1
        assert result[0][0] == b"\xbe\xef"


# ── VP9Packetizer ────────────────────────────────────────────────


class TestVP9PacketizerSinglePacket:
    def test_small_frame(self) -> None:
        pkt = VP9Packetizer()
        result = pkt.packetize(b"\x01\x02\x03", keyframe=True)
        assert len(result) == 1
        payload, marker = result[0]
        assert marker is True
        # Descriptor: B=1, E=1, P=0 (keyframe)
        assert payload[0] & _B_BIT
        assert payload[0] & _E_BIT
        assert not (payload[0] & _P_BIT)
        assert payload[1:] == b"\x01\x02\x03"

    def test_inter_frame_p_bit_set(self) -> None:
        pkt = VP9Packetizer()
        result = pkt.packetize(b"\x01", keyframe=False)
        assert len(result) == 1
        assert result[0][0][0] & _P_BIT


class TestVP9PacketizerFragmented:
    def test_large_frame_fragmented(self) -> None:
        pkt = VP9Packetizer()
        frame = bytes(range(256)) * 10  # 2560 bytes
        result = pkt.packetize(frame, mtu=1000, keyframe=False)
        assert len(result) >= 3

        # First has B=1
        assert result[0][0][0] & _B_BIT
        assert not (result[0][0][0] & _E_BIT)
        assert result[0][1] is False

        # Middle has neither B nor E
        for payload, marker in result[1:-1]:
            assert not (payload[0] & _B_BIT)
            assert not (payload[0] & _E_BIT)
            assert marker is False

        # Last has E=1
        assert result[-1][0][0] & _E_BIT
        assert not (result[-1][0][0] & _B_BIT)
        assert result[-1][1] is True

    def test_empty_frame(self) -> None:
        pkt = VP9Packetizer()
        assert pkt.packetize(b"") == []


# ── Roundtrip ────────────────────────────────────────────────────


class TestVP9Roundtrip:
    def test_small_frame_roundtrip(self) -> None:
        pkt = VP9Packetizer()
        depkt = VP9Depacketizer()
        original = b"\x01\x02\x03\x04\x05"

        packets = pkt.packetize(original, keyframe=True)
        frames: list[tuple[bytes, bool]] = []
        for payload, marker in packets:
            frames.extend(depkt.feed(payload, marker))

        assert len(frames) == 1
        assert frames[0][0] == original
        assert frames[0][1] is True

    def test_large_frame_roundtrip(self) -> None:
        pkt = VP9Packetizer()
        depkt = VP9Depacketizer()
        original = bytes(range(256)) * 20  # 5120 bytes

        packets = pkt.packetize(original, mtu=1000, keyframe=False)
        frames: list[tuple[bytes, bool]] = []
        for payload, marker in packets:
            frames.extend(depkt.feed(payload, marker))

        assert len(frames) == 1
        assert frames[0][0] == original
        assert frames[0][1] is False

    def test_multiple_frames_roundtrip(self) -> None:
        pkt = VP9Packetizer()
        depkt = VP9Depacketizer()

        frame1 = b"\x01" * 100
        frame2 = b"\x02" * 200

        all_frames: list[tuple[bytes, bool]] = []
        for payload, marker in pkt.packetize(frame1, keyframe=True):
            all_frames.extend(depkt.feed(payload, marker))
        for payload, marker in pkt.packetize(frame2, keyframe=False):
            all_frames.extend(depkt.feed(payload, marker))

        assert len(all_frames) == 2
        assert all_frames[0] == (frame1, True)
        assert all_frames[1] == (frame2, False)
