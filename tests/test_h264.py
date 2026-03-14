"""Tests for H.264 RTP packetization/depacketization (RFC 6184)."""

from __future__ import annotations

from aiortp.h264 import (
    _FU_A,
    _FU_E_BIT,
    _FU_S_BIT,
    _STAP_A,
    H264Depacketizer,
    H264Packetizer,
    is_keyframe_nal,
)

# ── is_keyframe_nal ──────────────────────────────────────────


class TestIsKeyframeNal:
    def test_idr_slice(self) -> None:
        # NAL type 5 = IDR slice
        assert is_keyframe_nal(bytes([0x65, 0x00])) is True

    def test_sps(self) -> None:
        # NAL type 7 = SPS
        assert is_keyframe_nal(bytes([0x67, 0x42, 0x00])) is True

    def test_pps(self) -> None:
        # NAL type 8 = PPS
        assert is_keyframe_nal(bytes([0x68, 0x00])) is True

    def test_non_idr_slice(self) -> None:
        # NAL type 1 = non-IDR slice
        assert is_keyframe_nal(bytes([0x41, 0x00])) is False

    def test_empty(self) -> None:
        assert is_keyframe_nal(b"") is False


# ── H264Depacketizer ─────────────────────────────────────────


class TestDepacketizerSingleNal:
    def test_single_nal_passthrough(self) -> None:
        """Single NAL unit (types 1-23) passed through as-is."""
        depkt = H264Depacketizer()
        # NAL type 1 (non-IDR slice), NRI=2
        nal = bytes([0x41]) + b"\x00" * 100
        result = depkt.feed(nal, marker=True)
        assert result == [nal]

    def test_single_nal_idr(self) -> None:
        depkt = H264Depacketizer()
        nal = bytes([0x65]) + b"\xab" * 50
        result = depkt.feed(nal, marker=True)
        assert result == [nal]

    def test_empty_payload(self) -> None:
        depkt = H264Depacketizer()
        assert depkt.feed(b"", marker=False) == []


class TestDepacketizerStapA:
    def test_two_nals_aggregated(self) -> None:
        """STAP-A unpacks multiple NAL units."""
        depkt = H264Depacketizer()
        nal1 = bytes([0x67, 0x42, 0x00, 0x1E])  # SPS
        nal2 = bytes([0x68, 0xCE, 0x38, 0x80])  # PPS

        # Build STAP-A: header(24) + size1(2) + nal1 + size2(2) + nal2
        stap = bytes([_STAP_A])
        stap += len(nal1).to_bytes(2, "big") + nal1
        stap += len(nal2).to_bytes(2, "big") + nal2

        result = depkt.feed(stap, marker=True)
        assert result == [nal1, nal2]

    def test_single_nal_in_stap(self) -> None:
        depkt = H264Depacketizer()
        nal = bytes([0x41, 0x00, 0x01])
        stap = bytes([_STAP_A]) + len(nal).to_bytes(2, "big") + nal

        result = depkt.feed(stap, marker=True)
        assert result == [nal]

    def test_truncated_stap(self) -> None:
        """Truncated STAP-A stops gracefully."""
        depkt = H264Depacketizer()
        nal = bytes([0x41, 0x00])
        # Claim size=10 but only provide 2 bytes
        stap = bytes([_STAP_A]) + (10).to_bytes(2, "big") + nal

        result = depkt.feed(stap, marker=True)
        assert result == []  # truncated, nothing delivered


class TestDepacketizerFuA:
    def _make_fu_a(
        self,
        nal_type: int,
        nri: int,
        data: bytes,
        *,
        start: bool = False,
        end: bool = False,
    ) -> bytes:
        """Build an FU-A payload."""
        fu_indicator = (nri << 5) | _FU_A
        fu_header = nal_type
        if start:
            fu_header |= _FU_S_BIT
        if end:
            fu_header |= _FU_E_BIT
        return bytes([fu_indicator, fu_header]) + data

    def test_two_fragment_reassembly(self) -> None:
        """FU-A start + end reassembles complete NAL."""
        depkt = H264Depacketizer()

        frag1 = self._make_fu_a(5, 3, b"AAAA", start=True)
        frag2 = self._make_fu_a(5, 3, b"BBBB", end=True)

        result1 = depkt.feed(frag1, marker=False)
        assert result1 == []

        result2 = depkt.feed(frag2, marker=True)
        assert len(result2) == 1

        # Reconstructed NAL: header(NRI=3, type=5) + data
        nal = result2[0]
        expected_header = (3 << 5) | 5  # NRI=3, type=5 (IDR)
        assert nal[0] == expected_header
        assert nal[1:] == b"AAAABBBB"

    def test_three_fragment_reassembly(self) -> None:
        """FU-A start + middle + end."""
        depkt = H264Depacketizer()

        frag1 = self._make_fu_a(1, 2, b"AA", start=True)
        frag2 = self._make_fu_a(1, 2, b"BB")  # middle
        frag3 = self._make_fu_a(1, 2, b"CC", end=True)

        assert depkt.feed(frag1, marker=False) == []
        assert depkt.feed(frag2, marker=False) == []
        result = depkt.feed(frag3, marker=True)

        assert len(result) == 1
        nal = result[0]
        expected_header = (2 << 5) | 1
        assert nal[0] == expected_header
        assert nal[1:] == b"AABBCC"

    def test_middle_without_start_dropped(self) -> None:
        """Middle/end fragments without a start are discarded."""
        depkt = H264Depacketizer()

        frag_mid = self._make_fu_a(1, 2, b"BB")
        frag_end = self._make_fu_a(1, 2, b"CC", end=True)

        assert depkt.feed(frag_mid, marker=False) == []
        assert depkt.feed(frag_end, marker=True) == []

    def test_reset_clears_fragment(self) -> None:
        """Reset discards in-progress FU-A fragment."""
        depkt = H264Depacketizer()

        frag1 = self._make_fu_a(5, 3, b"AAAA", start=True)
        depkt.feed(frag1, marker=False)

        depkt.reset()

        frag2 = self._make_fu_a(5, 3, b"BBBB", end=True)
        result = depkt.feed(frag2, marker=True)
        assert result == []  # fragment was reset

    def test_new_start_replaces_incomplete(self) -> None:
        """A new start fragment replaces an incomplete previous one."""
        depkt = H264Depacketizer()

        frag1 = self._make_fu_a(1, 2, b"old", start=True)
        depkt.feed(frag1, marker=False)

        # New start without finishing the old one
        frag2 = self._make_fu_a(5, 3, b"new_start", start=True)
        depkt.feed(frag2, marker=False)

        frag3 = self._make_fu_a(5, 3, b"_end", end=True)
        result = depkt.feed(frag3, marker=True)

        assert len(result) == 1
        nal = result[0]
        expected_header = (3 << 5) | 5
        assert nal[0] == expected_header
        assert nal[1:] == b"new_start_end"


# ── H264Packetizer ───────────────────────────────────────────


class TestPacketizerSingleNal:
    def test_small_nal_passthrough(self) -> None:
        """NAL smaller than MTU sent as single packet."""
        pkt = H264Packetizer()
        nal = bytes([0x65]) + b"\x00" * 100
        result = pkt.packetize(nal, mtu=1400)

        assert len(result) == 1
        payload, marker = result[0]
        assert payload == nal
        assert marker is True  # last (only) packet of this NAL unit

    def test_exact_mtu_no_fragmentation(self) -> None:
        pkt = H264Packetizer()
        nal = bytes([0x41]) + b"\x00" * 99
        result = pkt.packetize(nal, mtu=100)

        assert len(result) == 1
        assert result[0][0] == nal

    def test_empty_nal(self) -> None:
        pkt = H264Packetizer()
        assert pkt.packetize(b"") == []


class TestPacketizerFuA:
    def test_large_nal_fragmented(self) -> None:
        """NAL larger than MTU fragmented into FU-A packets."""
        pkt = H264Packetizer()
        nal_header = 0x65  # NRI=3, type=5 (IDR)
        nal = bytes([nal_header]) + b"\xab" * 200
        result = pkt.packetize(nal, mtu=100)

        # 200 bytes of data (after NAL header) with 98 bytes per fragment = 3 fragments
        assert len(result) == 3

        # Check first fragment (start)
        payload, _ = result[0]
        fu_indicator = payload[0]
        fu_header = payload[1]
        assert fu_indicator & 0x1F == _FU_A  # type = 28
        assert (fu_indicator >> 5) & 0x03 == 3  # NRI preserved
        assert fu_header & _FU_S_BIT  # start bit
        assert not (fu_header & _FU_E_BIT)  # no end bit
        assert fu_header & 0x1F == 5  # original NAL type

        # Check middle fragment
        payload, _ = result[1]
        fu_header = payload[1]
        assert not (fu_header & _FU_S_BIT)
        assert not (fu_header & _FU_E_BIT)

        # Check last fragment (end)
        payload, _ = result[2]
        fu_header = payload[1]
        assert not (fu_header & _FU_S_BIT)
        assert fu_header & _FU_E_BIT  # end bit

    def test_each_fragment_within_mtu(self) -> None:
        """Every FU-A fragment respects the MTU limit."""
        pkt = H264Packetizer()
        mtu = 50
        nal = bytes([0x41]) + b"\xff" * 300
        result = pkt.packetize(nal, mtu=mtu)

        for payload, _ in result:
            assert len(payload) <= mtu


# ── Roundtrip ─────────────────────────────────────────────────


class TestRoundtrip:
    def test_single_nal_roundtrip(self) -> None:
        """Packetize → depacketize for a small NAL."""
        original = bytes([0x41]) + b"\x01\x02\x03\x04"

        pkt = H264Packetizer()
        depkt = H264Depacketizer()

        packets = pkt.packetize(original, mtu=1400)
        nals: list[bytes] = []
        for payload, _ in packets:
            nals.extend(depkt.feed(payload, marker=True))

        assert nals == [original]

    def test_fu_a_roundtrip(self) -> None:
        """Packetize → depacketize for a large NAL using FU-A."""
        original = bytes([0x65]) + b"\xab" * 500

        pkt = H264Packetizer()
        depkt = H264Depacketizer()

        packets = pkt.packetize(original, mtu=100)
        assert len(packets) > 1  # must fragment

        nals: list[bytes] = []
        for i, (payload, _) in enumerate(packets):
            is_last = i == len(packets) - 1
            nals.extend(depkt.feed(payload, marker=is_last))

        assert len(nals) == 1
        assert nals[0] == original

    def test_multi_nal_frame_roundtrip(self) -> None:
        """Multiple NALs (SPS + PPS + IDR) through packetize/depacketize."""
        sps = bytes([0x67, 0x42, 0x00, 0x1E])
        pps = bytes([0x68, 0xCE, 0x38, 0x80])
        idr = bytes([0x65]) + b"\x00" * 2000  # large IDR, will be fragmented

        pkt = H264Packetizer()
        depkt = H264Depacketizer()

        all_packets: list[tuple[bytes, bool]] = []
        for nal in [sps, pps, idr]:
            all_packets.extend(pkt.packetize(nal, mtu=1400))

        recovered: list[bytes] = []
        for i, (payload, _) in enumerate(all_packets):
            is_last = i == len(all_packets) - 1
            recovered.extend(depkt.feed(payload, marker=is_last))

        assert recovered == [sps, pps, idr]
