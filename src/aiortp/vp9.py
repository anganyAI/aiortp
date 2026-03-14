"""VP9 RTP depacketization and packetization (RFC 9628).

Supports the VP9 RTP payload descriptor format with optional
Picture ID, layer indices, and flexible-mode reference indices.

Frame boundaries are detected via B (begin) and E (end) bits in
the mandatory first octet.  Keyframes are identified by P=0
(no inter-picture prediction).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Mandatory first-octet bit masks
_I_BIT = 0x80  # Picture ID present
_P_BIT = 0x40  # Inter-picture predicted (0 = keyframe)
_L_BIT = 0x20  # Layer indices present
_F_BIT = 0x10  # Flexible mode
_B_BIT = 0x08  # Beginning of frame
_E_BIT = 0x04  # End of frame
_V_BIT = 0x02  # Scalability structure present

# Picture ID extension
_M_BIT = 0x80  # 15-bit PID if set, 7-bit if clear

# Reference index chain
_N_BIT = 0x80  # Another reference index follows


def is_keyframe_vp9(payload: bytes) -> bool:
    """Check if a VP9 RTP payload starts a keyframe.

    A keyframe has P=0 (no inter-picture prediction) and B=1
    (beginning of frame).
    """
    if len(payload) < 1:
        return False
    first = payload[0]
    return not (first & _P_BIT) and bool(first & _B_BIT)


def _parse_descriptor_offset(payload: bytes) -> int:
    """Return the byte offset where VP9 frame data begins.

    Skips the mandatory first octet and all optional fields
    (Picture ID, layer indices, reference indices, scalability
    structure) based on the flag bits.
    """
    if len(payload) < 1:
        return 0

    first = payload[0]
    offset = 1

    # Picture ID (if I=1)
    if first & _I_BIT:
        if offset >= len(payload):
            return offset
        if payload[offset] & _M_BIT:
            offset += 2  # 15-bit PID
        else:
            offset += 1  # 7-bit PID

    # Layer indices (if L=1): TID/U/SID/D byte, plus TL0PICIDX
    # byte in non-flexible mode (F=0) per RFC 9628 Section 4.2
    if first & _L_BIT:
        offset += 1  # TID/U/SID/D
        if not (first & _F_BIT):
            offset += 1  # TL0PICIDX (non-flexible mode only)

    # Reference indices (if F=1 and P=1): variable length
    if (first & _F_BIT) and (first & _P_BIT):
        # Chain of reference indices, each 1 byte, N bit continues
        while offset < len(payload):
            ref_byte = payload[offset]
            offset += 1
            if not (ref_byte & _N_BIT):
                break

    # Scalability structure (if V=1): variable length
    if first & _V_BIT:
        offset = _skip_scalability_structure(payload, offset)

    return min(offset, len(payload))


def _skip_scalability_structure(payload: bytes, offset: int) -> int:
    """Skip the scalability structure (SS) data.

    The SS header is: N_S (3 bits) | Y (1 bit) | G (1 bit) | reserved (3 bits)
    Followed by optional resolution fields and picture group descriptions.
    """
    if offset >= len(payload):
        return offset

    ss_byte = payload[offset]
    offset += 1
    n_s = (ss_byte >> 5) + 1  # Number of spatial layers (1-8)
    y_bit = bool(ss_byte & 0x10)  # Resolution present
    g_bit = bool(ss_byte & 0x08)  # PG description present

    # Resolution: 4 bytes per spatial layer (width_16 + height_16)
    if y_bit:
        offset += n_s * 4
        if offset >= len(payload):
            return offset

    # Picture group descriptions
    if g_bit:
        if offset >= len(payload):
            return offset
        n_g = payload[offset]
        offset += 1
        for _ in range(n_g):
            if offset >= len(payload):
                return offset
            pg_byte = payload[offset]
            offset += 1
            # R (2 bits) | TID (3 bits) | U (1 bit) | num_ref (2 bits)
            num_ref = pg_byte & 0x03
            offset += num_ref  # Each P_DIFF is 1 byte

    return offset


class VP9Depacketizer:
    """Reassemble VP9 frames from RTP packets (RFC 9628).

    Unlike H.264 FU-A, VP9 uses B/E bits in the payload descriptor
    to mark frame boundaries.  Frame data is simply concatenated
    from B=1 through E=1.
    """

    def __init__(self) -> None:
        self._frame_buffer: bytearray | None = None
        self._is_keyframe: bool = False

    def feed(self, payload: bytes, marker: bool) -> list[tuple[bytes, bool]]:
        """Feed one RTP payload, return completed frames.

        Args:
            payload: RTP payload bytes (without RTP header).
            marker: RTP marker bit (ignored for VP9 — E bit is used).

        Returns:
            List of ``(frame_data, is_keyframe)`` tuples.
            Usually 0 or 1 entries.
        """
        if len(payload) < 1:
            return []

        first = payload[0]
        b_start = bool(first & _B_BIT)
        e_end = bool(first & _E_BIT)
        is_inter = bool(first & _P_BIT)

        data_offset = _parse_descriptor_offset(payload)
        frame_data = payload[data_offset:]

        if b_start:
            self._frame_buffer = bytearray(frame_data)
            self._is_keyframe = not is_inter
        elif self._frame_buffer is not None:
            self._frame_buffer.extend(frame_data)
        else:
            # Middle/end packet without a start — discard
            return []

        if e_end and self._frame_buffer is not None:
            complete = bytes(self._frame_buffer)
            keyframe = self._is_keyframe
            self._frame_buffer = None
            self._is_keyframe = False
            return [(complete, keyframe)]

        return []

    def reset(self) -> None:
        """Discard any in-progress frame assembly."""
        self._frame_buffer = None
        self._is_keyframe = False


class VP9Packetizer:
    """Fragment VP9 frames into RTP-sized payloads (RFC 9628)."""

    def packetize(
        self,
        frame: bytes,
        mtu: int = 1400,
        keyframe: bool = False,
    ) -> list[tuple[bytes, bool]]:
        """Split a VP9 frame into MTU-sized RTP payloads.

        Args:
            frame: Complete VP9 frame bitstream.
            mtu: Maximum payload size per RTP packet.
            keyframe: Whether this is a keyframe.

        Returns:
            List of ``(payload, marker)`` tuples.  Marker is True
            on the last packet of the frame.
        """
        if not frame:
            return []

        # P bit: 0 for keyframe, 1 for inter-frame
        p_flag = 0 if keyframe else _P_BIT

        # Single packet (fits in MTU with 1-byte descriptor)
        if len(frame) + 1 <= mtu:
            descriptor = p_flag | _B_BIT | _E_BIT
            return [(bytes([descriptor]) + frame, True)]

        # Fragment across multiple packets
        max_payload = mtu - 1  # 1-byte descriptor overhead
        fragments: list[tuple[bytes, bool]] = []
        offset = 0

        while offset < len(frame):
            end = min(offset + max_payload, len(frame))
            is_first = offset == 0
            is_last = end == len(frame)

            descriptor = p_flag
            if is_first:
                descriptor |= _B_BIT
            if is_last:
                descriptor |= _E_BIT

            payload = bytes([descriptor]) + frame[offset:end]
            fragments.append((payload, is_last))
            offset = end

        return fragments
