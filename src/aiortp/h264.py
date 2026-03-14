"""H.264 RTP packetization and depacketization (RFC 6184).

Supports packetization-mode 1 (non-interleaved) — the standard mode
for SIP video calls.  Handles three NAL unit types:

- Single NAL (type 1-23): passed through as-is
- STAP-A (type 24): aggregated NALs unpacked into individual units
- FU-A (type 28): fragmented NAL reassembled from start/end markers
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# NAL unit type constants
_NAL_TYPE_MASK = 0x1F
_NAL_NRI_MASK = 0x60
_NAL_F_MASK = 0x80

_STAP_A = 24
_FU_A = 28

# FU header bits
_FU_S_BIT = 0x80  # start
_FU_E_BIT = 0x40  # end

# NAL types that indicate keyframes (IDR)
_IDR_SLICE = 5
_SPS = 7
_PPS = 8


def is_keyframe_nal(nal_unit: bytes) -> bool:
    """Check if a NAL unit belongs to a keyframe (IDR, SPS, or PPS)."""
    if not nal_unit:
        return False
    nal_type = nal_unit[0] & _NAL_TYPE_MASK
    return nal_type in (_IDR_SLICE, _SPS, _PPS)


class H264Depacketizer:
    """Reassemble H.264 NAL units from RTP packets (RFC 6184)."""

    def __init__(self) -> None:
        self._fua_buffer: bytearray | None = None
        self._fua_nal_header: int = 0
        self._unsupported_warned: set[int] = set()

    def feed(self, payload: bytes, marker: bool) -> list[bytes]:
        """Feed one RTP payload, return complete NAL units (if any).

        Args:
            payload: RTP payload bytes (without RTP header).
            marker: RTP marker bit — indicates last packet of access unit.

        Returns:
            List of complete NAL units. Empty if fragment is incomplete.
        """
        if len(payload) < 1:
            return []

        nal_type = payload[0] & _NAL_TYPE_MASK

        if nal_type in range(1, 24):
            return self._handle_single_nal(payload)
        if nal_type == _STAP_A:
            return self._handle_stap_a(payload)
        if nal_type == _FU_A:
            return self._handle_fu_a(payload)

        if nal_type not in self._unsupported_warned:
            self._unsupported_warned.add(nal_type)
            logger.warning("Unsupported NAL unit type %d", nal_type)
        return []

    def reset(self) -> None:
        """Discard any in-progress FU-A fragment."""
        self._fua_buffer = None
        self._fua_nal_header = 0
        self._unsupported_warned.clear()

    def _handle_single_nal(self, payload: bytes) -> list[bytes]:
        """Single NAL unit packet (types 1-23) — return as-is."""
        return [bytes(payload)]

    def _handle_stap_a(self, payload: bytes) -> list[bytes]:
        """STAP-A (type 24) — unpack aggregated NAL units."""
        nals: list[bytes] = []
        pos = 1  # skip STAP-A header byte

        while pos + 2 <= len(payload):
            size = (payload[pos] << 8) | payload[pos + 1]
            pos += 2
            if pos + size > len(payload):
                logger.warning("STAP-A NAL unit truncated at offset %d", pos)
                break
            nals.append(bytes(payload[pos : pos + size]))
            pos += size

        return nals

    def _handle_fu_a(self, payload: bytes) -> list[bytes]:
        """FU-A (type 28) — reassemble fragmented NAL unit."""
        if len(payload) < 2:
            return []

        fu_indicator = payload[0]
        fu_header = payload[1]
        is_start = bool(fu_header & _FU_S_BIT)
        is_end = bool(fu_header & _FU_E_BIT)

        if is_start:
            # Reconstruct NAL header: F and NRI from FU indicator, Type from FU header
            nal_header = (fu_indicator & (_NAL_F_MASK | _NAL_NRI_MASK)) | (
                fu_header & _NAL_TYPE_MASK
            )
            self._fua_nal_header = nal_header
            self._fua_buffer = bytearray([nal_header])
            self._fua_buffer.extend(payload[2:])
            return []

        if self._fua_buffer is None:
            # Middle/end fragment without start — discard
            return []

        self._fua_buffer.extend(payload[2:])

        if is_end:
            nal = bytes(self._fua_buffer)
            self._fua_buffer = None
            return [nal]

        return []


class H264Packetizer:
    """Fragment H.264 NAL units into RTP-sized payloads (RFC 6184)."""

    def packetize(self, nal_unit: bytes, mtu: int = 1400) -> list[tuple[bytes, bool]]:
        """Split a NAL unit into MTU-sized RTP payloads.

        Args:
            nal_unit: Complete NAL unit (with NAL header byte).
            mtu: Maximum payload size per RTP packet.

        Returns:
            List of ``(payload, is_last)`` tuples.  ``is_last`` is True
            on the final packet produced for this NAL unit.  For
            multi-NAL access units the caller must override the RTP
            marker bit so that only the very last packet of the entire
            access unit carries marker=1.
        """
        if not nal_unit:
            return []

        if len(nal_unit) <= mtu:
            # Single NAL unit packet — fits in one RTP payload
            return [(nal_unit, True)]

        # FU-A fragmentation
        return self._fragment_fu_a(nal_unit, mtu)

    def _fragment_fu_a(self, nal_unit: bytes, mtu: int) -> list[tuple[bytes, bool]]:
        """Fragment a NAL unit using FU-A (type 28)."""
        nal_header = nal_unit[0]
        nal_type = nal_header & _NAL_TYPE_MASK

        # FU indicator: same F and NRI as original, type = 28
        fu_indicator = (nal_header & (_NAL_F_MASK | _NAL_NRI_MASK)) | _FU_A

        # Payload to fragment (everything after NAL header)
        data = nal_unit[1:]
        # Each fragment has 2-byte FU header overhead
        max_fragment = mtu - 2
        fragments: list[tuple[bytes, bool]] = []

        offset = 0
        while offset < len(data):
            end = min(offset + max_fragment, len(data))
            is_start = offset == 0
            is_end = end == len(data)

            fu_header = nal_type
            if is_start:
                fu_header |= _FU_S_BIT
            if is_end:
                fu_header |= _FU_E_BIT

            payload = bytes([fu_indicator, fu_header]) + data[offset:end]
            fragments.append((payload, is_end))
            offset = end

        return fragments
