"""VP8 RTP depacketization and packetization (RFC 7741).

Supports the VP8 RTP payload descriptor format with optional
Picture ID, TL0PICIDX, and TID/KEYIDX fields.

Frame boundaries are detected via S bit (start of partition)
and the RTP marker bit (end of frame).  Keyframes are identified
from the VP8 bitstream frame tag (bit 0 of first payload byte = 0).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Mandatory first-octet bit masks
_X_BIT = 0x80  # Extended control bits present
_N_BIT = 0x20  # Non-reference frame
_S_BIT = 0x10  # Start of VP8 partition
_PID_MASK = 0x0F  # Partition index

# Extension byte bit masks (present when X=1)
_I_BIT = 0x80  # Picture ID present
_L_BIT = 0x40  # TL0PICIDX present
_T_BIT = 0x20  # TID present
_K_BIT = 0x10  # KEYIDX present

# Picture ID extension
_M_BIT = 0x80  # 15-bit PID if set, 7-bit if clear


def is_keyframe_vp8(payload: bytes) -> bool:
    """Check if a VP8 RTP payload starts a keyframe.

    A keyframe has S=1, PartID=0, and the VP8 bitstream frame tag
    first bit = 0 (inverse video flag).
    """
    if len(payload) < 1:
        return False
    first = payload[0]
    if not (first & _S_BIT) or (first & _PID_MASK) != 0:
        return False
    offset = _parse_descriptor_offset(payload)
    if offset >= len(payload):
        return False
    # VP8 frame tag: bit 0 = 0 means keyframe
    return not (payload[offset] & 0x01)


def _parse_descriptor_offset(payload: bytes) -> int:
    """Return the byte offset where VP8 frame data begins.

    Skips the mandatory first octet and all optional fields
    (extension byte, Picture ID, TL0PICIDX, TID/KEYIDX).
    """
    if len(payload) < 1:
        return 0

    first = payload[0]
    offset = 1

    # Extension byte (if X=1)
    if not (first & _X_BIT):
        return offset

    if offset >= len(payload):
        return offset
    ext = payload[offset]
    offset += 1

    # Picture ID (if I=1)
    if ext & _I_BIT:
        if offset >= len(payload):
            return offset
        if payload[offset] & _M_BIT:
            offset += 2  # 15-bit PID
        else:
            offset += 1  # 7-bit PID

    # TL0PICIDX (if L=1)
    if ext & _L_BIT:
        offset += 1

    # TID/Y/KEYIDX (if T=1 or K=1)
    if (ext & _T_BIT) or (ext & _K_BIT):
        offset += 1

    return min(offset, len(payload))


class VP8Depacketizer:
    """Reassemble VP8 frames from RTP packets (RFC 7741).

    Uses S bit for frame start and RTP marker bit for frame end.
    """

    def __init__(self) -> None:
        self._frame_buffer: bytearray | None = None
        self._is_keyframe: bool = False

    def feed(self, payload: bytes, marker: bool) -> list[tuple[bytes, bool]]:
        """Feed one RTP payload, return completed frames.

        Args:
            payload: RTP payload bytes (without RTP header).
            marker: RTP marker bit — True on last packet of frame.

        Returns:
            List of ``(frame_data, is_keyframe)`` tuples.
            Usually 0 or 1 entries.
        """
        if len(payload) < 1:
            return []

        first = payload[0]
        s_start = bool(first & _S_BIT)

        data_offset = _parse_descriptor_offset(payload)
        frame_data = payload[data_offset:]

        if s_start:
            self._frame_buffer = bytearray(frame_data)
            self._is_keyframe = is_keyframe_vp8(payload)
        elif self._frame_buffer is not None:
            self._frame_buffer.extend(frame_data)
        else:
            # Continuation without start — discard
            return []

        if marker and self._frame_buffer is not None:
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


class VP8Packetizer:
    """Fragment VP8 frames into RTP-sized payloads (RFC 7741)."""

    def packetize(
        self, frame: bytes, mtu: int = 1400, keyframe: bool = False,
    ) -> list[tuple[bytes, bool]]:
        """Split a VP8 frame into MTU-sized RTP payloads.

        Args:
            frame: Complete VP8 frame bitstream.
            mtu: Maximum payload size per RTP packet.
            keyframe: Whether this is a keyframe (unused in descriptor,
                but returned in the is_last flag for caller convenience).

        Returns:
            List of ``(payload, is_last)`` tuples.  ``is_last`` is True
            on the last packet of the frame.
        """
        if not frame:
            return []

        # Single packet (fits in MTU with 1-byte descriptor)
        if len(frame) + 1 <= mtu:
            descriptor = _S_BIT  # S=1, PartID=0
            return [(bytes([descriptor]) + frame, True)]

        # Fragment across multiple packets
        max_payload = mtu - 1  # 1-byte descriptor overhead
        fragments: list[tuple[bytes, bool]] = []
        offset = 0

        while offset < len(frame):
            end = min(offset + max_payload, len(frame))
            is_first = offset == 0
            is_last = end == len(frame)

            descriptor = 0
            if is_first:
                descriptor |= _S_BIT  # S=1 on first packet only

            payload = bytes([descriptor]) + frame[offset:end]
            fragments.append((payload, is_last))
            offset = end

        return fragments
