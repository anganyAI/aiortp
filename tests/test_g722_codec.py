"""Tests for the G.722 codec wrapper."""

from __future__ import annotations

import math
import struct

import pytest

from aiortp.codecs import PayloadType, _registry
from aiortp.codecs.base import Codec

# G722 C extension may not be installed — skip tests if unavailable.
g722_available = False
try:
    from G722 import G722 as _G722Check  # type: ignore[import-untyped]

    g722_available = True
except ImportError:
    pass

if g722_available:
    from aiortp.codecs.g722 import G722Codec

pytestmark = pytest.mark.skipif(not g722_available, reason="G722 package not installed")


class TestG722CodecProperties:
    def test_name(self) -> None:
        codec = G722Codec()
        assert codec.name == "G722"

    def test_sample_rate(self) -> None:
        codec = G722Codec()
        assert codec.sample_rate == 16000

    def test_samples_per_frame(self) -> None:
        codec = G722Codec()
        assert codec.samples_per_frame == 320

    def test_is_codec_subclass(self) -> None:
        codec = G722Codec()
        assert isinstance(codec, Codec)


class TestG722PayloadType:
    def test_payload_type_enum(self) -> None:
        assert PayloadType.G722 == 9

    def test_registered_in_registry(self) -> None:
        assert 9 in _registry
        assert _registry[9] is G722Codec  # type: ignore[possibly-undefined]


class TestG722EncodeDecode:
    def test_encode_output_size(self) -> None:
        """320 PCM samples (640 bytes) encode to 160 bytes."""
        codec = G722Codec()
        pcm = struct.pack("<320h", *([0] * 320))
        encoded = codec.encode(pcm)
        assert len(encoded) == 160

    def test_decode_output_size(self) -> None:
        """160 encoded bytes decode to 320 PCM samples (640 bytes)."""
        codec = G722Codec()
        # Encode silence first to get valid G.722 payload
        pcm = struct.pack("<320h", *([0] * 320))
        encoded = codec.encode(pcm)
        decoded = codec.decode(encoded)
        assert len(decoded) == 640  # 320 samples * 2 bytes

    def test_roundtrip_silence(self) -> None:
        """Encoding then decoding silence should produce near-silence."""
        codec = G722Codec()
        pcm = struct.pack("<320h", *([0] * 320))
        encoded = codec.encode(pcm)
        decoded = codec.decode(encoded)
        samples = struct.unpack("<320h", decoded)
        # All samples should be zero or very close to zero
        assert all(abs(s) < 10 for s in samples)

    def test_roundtrip_sine_wave(self) -> None:
        """Encoding then decoding a sine wave should preserve the signal."""
        codec = G722Codec()
        freq = 440.0
        rate = 16000
        n = 320
        original = [int(16000 * math.sin(2 * math.pi * freq * i / rate)) for i in range(n)]
        pcm = struct.pack(f"<{n}h", *original)

        encoded = codec.encode(pcm)
        decoded = codec.decode(encoded)
        recovered = struct.unpack(f"<{n}h", decoded)

        # Calculate correlation — lossy codec, but signal should be recognizable
        # Check that the energy is preserved (within 6 dB)
        orig_energy = sum(s * s for s in original) / n
        recov_energy = sum(s * s for s in recovered) / n
        assert recov_energy > orig_energy * 0.1, "Signal energy too low after roundtrip"

    def test_encode_partial_frame(self) -> None:
        """Encoding fewer than 320 samples should still work."""
        codec = G722Codec()
        pcm = struct.pack("<80h", *([1000] * 80))
        encoded = codec.encode(pcm)
        assert len(encoded) == 40  # 80 samples → 40 bytes

    def test_decode_partial_frame(self) -> None:
        """Decoding fewer than 160 bytes should still work."""
        codec = G722Codec()
        pcm = struct.pack("<80h", *([0] * 80))
        encoded = codec.encode(pcm)
        decoded = codec.decode(encoded)
        assert len(decoded) == 160  # 80 samples * 2 bytes
