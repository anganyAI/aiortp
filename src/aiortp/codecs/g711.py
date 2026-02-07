"""Pure Python G.711 µ-law and A-law codecs with precomputed lookup tables."""

import struct

from .base import Codec

# --- µ-law (PCMU, G.711u) ---

ULAW_BIAS = 0x84
ULAW_CLIP = 32635

# Precompute encode table: signed 16-bit -> µ-law byte
_ULAW_ENCODE_TABLE: list[int] = []


def _build_ulaw_encode_table() -> list[int]:
    table = []
    for i in range(65536):
        sample = i if i < 32768 else i - 65536  # interpret as signed
        sign = 0
        if sample < 0:
            sign = 0x80
            sample = -sample
        if sample > ULAW_CLIP:
            sample = ULAW_CLIP
        sample += ULAW_BIAS

        exponent = 7
        mask = 0x4000
        while exponent > 0:
            if sample & mask:
                break
            exponent -= 1
            mask >>= 1

        mantissa = (sample >> (exponent + 3)) & 0x0F
        ulaw_byte = ~(sign | (exponent << 4) | mantissa) & 0xFF
        table.append(ulaw_byte)
    return table


_ULAW_ENCODE_TABLE = _build_ulaw_encode_table()

# Precompute decode table: µ-law byte -> signed 16-bit
_ULAW_DECODE_TABLE: list[int] = []


def _build_ulaw_decode_table() -> list[int]:
    table = []
    for i in range(256):
        ulaw = ~i & 0xFF
        sign = ulaw & 0x80
        exponent = (ulaw >> 4) & 0x07
        mantissa = ulaw & 0x0F
        sample = ((mantissa << 3) + ULAW_BIAS) << exponent
        sample -= ULAW_BIAS
        if sign:
            sample = -sample
        table.append(sample)
    return table


_ULAW_DECODE_TABLE = _build_ulaw_decode_table()


class PcmuCodec(Codec):
    @property
    def name(self) -> str:
        return "PCMU"

    @property
    def sample_rate(self) -> int:
        return 8000

    @property
    def samples_per_frame(self) -> int:
        return 160

    def encode(self, pcm: bytes) -> bytes:
        """Encode s16le PCM to µ-law."""
        result = bytearray(len(pcm) // 2)
        for i in range(0, len(pcm), 2):
            sample = struct.unpack_from("<h", pcm, i)[0]
            # Convert signed to unsigned index
            idx = sample & 0xFFFF
            result[i // 2] = _ULAW_ENCODE_TABLE[idx]
        return bytes(result)

    def decode(self, payload: bytes) -> bytes:
        """Decode µ-law to s16le PCM."""
        result = bytearray(len(payload) * 2)
        for i, byte in enumerate(payload):
            sample = _ULAW_DECODE_TABLE[byte]
            struct.pack_into("<h", result, i * 2, sample)
        return bytes(result)


# --- A-law (PCMA, G.711a) ---

# Precompute encode table: signed 16-bit -> A-law byte
_ALAW_ENCODE_TABLE: list[int] = []


def _build_alaw_encode_table() -> list[int]:
    table = []
    for i in range(65536):
        sample = i if i < 32768 else i - 65536  # interpret as signed
        sign = 0
        if sample < 0:
            sign = 0x80
            sample = -sample

        if sample > 32767:
            sample = 32767

        if sample >= 256:
            exponent = 7
            mask = 0x4000
            while exponent > 1:
                if sample & mask:
                    break
                exponent -= 1
                mask >>= 1
            mantissa = (sample >> (exponent + 3)) & 0x0F
            alaw_byte = (sign | (exponent << 4) | mantissa) ^ 0x55
        else:
            alaw_byte = (sign | (sample >> 4)) ^ 0x55

        table.append(alaw_byte)
    return table


_ALAW_ENCODE_TABLE = _build_alaw_encode_table()

# Precompute decode table: A-law byte -> signed 16-bit
_ALAW_DECODE_TABLE: list[int] = []


def _build_alaw_decode_table() -> list[int]:
    table = []
    for i in range(256):
        alaw = i ^ 0x55
        sign = alaw & 0x80
        exponent = (alaw >> 4) & 0x07
        mantissa = alaw & 0x0F

        if exponent == 0:
            sample = (mantissa << 4) + 8
        else:
            sample = ((mantissa << 4) + 0x108) << (exponent - 1)

        if sign:
            sample = -sample
        table.append(sample)
    return table


_ALAW_DECODE_TABLE = _build_alaw_decode_table()


class PcmaCodec(Codec):
    @property
    def name(self) -> str:
        return "PCMA"

    @property
    def sample_rate(self) -> int:
        return 8000

    @property
    def samples_per_frame(self) -> int:
        return 160

    def encode(self, pcm: bytes) -> bytes:
        """Encode s16le PCM to A-law."""
        result = bytearray(len(pcm) // 2)
        for i in range(0, len(pcm), 2):
            sample = struct.unpack_from("<h", pcm, i)[0]
            idx = sample & 0xFFFF
            result[i // 2] = _ALAW_ENCODE_TABLE[idx]
        return bytes(result)

    def decode(self, payload: bytes) -> bytes:
        """Decode A-law to s16le PCM."""
        result = bytearray(len(payload) * 2)
        for i, byte in enumerate(payload):
            sample = _ALAW_DECODE_TABLE[byte]
            struct.pack_into("<h", result, i * 2, sample)
        return bytes(result)
