"""Pure Python G.711 µ-law and A-law codecs with precomputed lookup tables.

Both directions run without a per-sample Python loop: encode maps each
16-bit sample through a 64 KiB table indexed by the raw sample bytes,
and decode runs two ``bytes.translate`` passes (low byte, high byte)
interleaved with slice assignment — all C-speed bulk operations.
"""

import sys
from collections.abc import Callable

from .base import Codec

ULAW_BIAS = 0x84
ULAW_CLIP = 32635


# --- Per-sample reference transforms (ITU-T G.711) ---


def _ulaw_encode_sample(sample: int) -> int:
    """Encode one signed 16-bit sample to a µ-law byte."""
    sign = 0
    if sample < 0:
        sign = 0x80
        sample = -sample
    if sample > ULAW_CLIP:
        sample = ULAW_CLIP
    sample += ULAW_BIAS

    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1

    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def _ulaw_decode_sample(byte: int) -> int:
    """Decode one µ-law byte to a signed 16-bit sample."""
    ulaw = ~byte & 0xFF
    sign = ulaw & 0x80
    exponent = (ulaw >> 4) & 0x07
    mantissa = ulaw & 0x0F
    sample = ((mantissa << 3) + ULAW_BIAS) << exponent
    sample -= ULAW_BIAS
    return -sample if sign else sample


def _alaw_encode_sample(sample: int) -> int:
    """Encode one signed 16-bit sample to an A-law byte."""
    sign = 0
    if sample < 0:
        sign = 0x80
        sample = -sample
    if sample > 32767:
        sample = 32767

    if sample >= 256:
        exponent = 7
        mask = 0x4000
        while exponent > 1 and not (sample & mask):
            exponent -= 1
            mask >>= 1
        mantissa = (sample >> (exponent + 3)) & 0x0F
        return (sign | (exponent << 4) | mantissa) ^ 0x55
    return (sign | (sample >> 4)) ^ 0x55


def _alaw_decode_sample(byte: int) -> int:
    """Decode one A-law byte to a signed 16-bit sample."""
    alaw = byte ^ 0x55
    sign = alaw & 0x80
    exponent = (alaw >> 4) & 0x07
    mantissa = alaw & 0x0F

    if exponent == 0:
        sample = (mantissa << 4) + 8
    else:
        sample = ((mantissa << 4) + 0x108) << (exponent - 1)

    return -sample if sign else sample


# --- Lookup table construction ---


def _build_encode_table(encode_sample: Callable[[int], int]) -> bytes:
    """Build a 64 KiB table: raw u16 sample (native order) -> codec byte.

    ``encode()`` reads the s16le input through a native-order u16 view,
    so each index is mapped back here to the little-endian sample value
    it represents.  On little-endian hosts the index *is* the sample.
    """
    swap = sys.byteorder == "big"
    table = bytearray(65536)
    for i in range(65536):
        value = ((i & 0xFF) << 8) | (i >> 8) if swap else i
        table[i] = encode_sample(value - 65536 if value >= 0x8000 else value)
    return bytes(table)


def _build_decode_tables(decode_sample: Callable[[int], int]) -> tuple[bytes, bytes]:
    """Build 256-entry translate tables: codec byte -> s16le low/high byte."""
    lo = bytearray(256)
    hi = bytearray(256)
    for i in range(256):
        lo[i], hi[i] = decode_sample(i).to_bytes(2, "little", signed=True)
    return bytes(lo), bytes(hi)


# --- Codecs ---


class _LutCodec(Codec):
    """G.711-family codec backed by the lookup tables above."""

    _encode_table: bytes
    _decode_lo: bytes
    _decode_hi: bytes

    @property
    def sample_rate(self) -> int:
        return 8000

    @property
    def samples_per_frame(self) -> int:
        return 160

    def encode(self, pcm: bytes) -> bytes:
        """Encode s16le PCM to codec bytes."""
        n = len(pcm) // 2
        samples = memoryview(pcm)[: n * 2].cast("H")
        return bytes(map(self._encode_table.__getitem__, samples))

    def decode(self, payload: bytes) -> bytes:
        """Decode codec bytes to s16le PCM."""
        out = bytearray(len(payload) * 2)
        out[0::2] = payload.translate(self._decode_lo)
        out[1::2] = payload.translate(self._decode_hi)
        return bytes(out)


class PcmuCodec(_LutCodec):
    _encode_table = _build_encode_table(_ulaw_encode_sample)
    _decode_lo, _decode_hi = _build_decode_tables(_ulaw_decode_sample)

    @property
    def name(self) -> str:
        return "PCMU"


class PcmaCodec(_LutCodec):
    _encode_table = _build_encode_table(_alaw_encode_sample)
    _decode_lo, _decode_hi = _build_decode_tables(_alaw_decode_sample)

    @property
    def name(self) -> str:
        return "PCMA"
