import struct
import unittest
from unittest import TestCase

from aiortp.codecs import PayloadType, get_codec
from aiortp.codecs.g711 import (
    PcmaCodec,
    PcmuCodec,
    _alaw_decode_sample,
    _alaw_encode_sample,
    _ulaw_decode_sample,
    _ulaw_encode_sample,
)
from aiortp.codecs.opus import _HAS_OPUS, OpusCodec
from aiortp.codecs.pcm import L16Codec


class PcmuCodecTest(TestCase):
    def test_roundtrip(self) -> None:
        codec = PcmuCodec()
        # Generate a simple PCM signal
        pcm = b""
        for i in range(160):
            sample = int(10000 * (1 if i % 2 == 0 else -1))
            pcm += struct.pack("<h", sample)

        encoded = codec.encode(pcm)
        self.assertEqual(len(encoded), 160)

        decoded = codec.decode(encoded)
        self.assertEqual(len(decoded), 320)

        # Check roundtrip is close (G.711 is lossy)
        for i in range(160):
            original = struct.unpack_from("<h", pcm, i * 2)[0]
            recovered = struct.unpack_from("<h", decoded, i * 2)[0]
            # µ-law has about 1% error for most values
            self.assertAlmostEqual(original, recovered, delta=abs(original * 0.05) + 16)

    def test_silence(self) -> None:
        codec = PcmuCodec()
        # Encode silence (all zeros)
        pcm = b"\x00" * 320  # 160 samples of silence
        encoded = codec.encode(pcm)
        decoded = codec.decode(encoded)
        # Decoded silence should be close to 0
        for i in range(160):
            sample = struct.unpack_from("<h", decoded, i * 2)[0]
            self.assertAlmostEqual(sample, 0, delta=8)

    def test_properties(self) -> None:
        codec = PcmuCodec()
        self.assertEqual(codec.name, "PCMU")
        self.assertEqual(codec.sample_rate, 8000)
        self.assertEqual(codec.samples_per_frame, 160)

    def test_conceal_returns_none(self) -> None:
        # G.711 has no native PLC — callers use the generic concealer
        self.assertIsNone(PcmuCodec().conceal(160))


class PcmaCodecTest(TestCase):
    def test_roundtrip(self) -> None:
        codec = PcmaCodec()
        pcm = b""
        for i in range(160):
            sample = int(10000 * (1 if i % 2 == 0 else -1))
            pcm += struct.pack("<h", sample)

        encoded = codec.encode(pcm)
        self.assertEqual(len(encoded), 160)

        decoded = codec.decode(encoded)
        self.assertEqual(len(decoded), 320)

        for i in range(160):
            original = struct.unpack_from("<h", pcm, i * 2)[0]
            recovered = struct.unpack_from("<h", decoded, i * 2)[0]
            self.assertAlmostEqual(original, recovered, delta=abs(original * 0.05) + 16)

    def test_silence(self) -> None:
        codec = PcmaCodec()
        pcm = b"\x00" * 320
        encoded = codec.encode(pcm)
        decoded = codec.decode(encoded)
        for i in range(160):
            sample = struct.unpack_from("<h", decoded, i * 2)[0]
            self.assertAlmostEqual(sample, 0, delta=16)

    def test_properties(self) -> None:
        codec = PcmaCodec()
        self.assertEqual(codec.name, "PCMA")
        self.assertEqual(codec.sample_rate, 8000)
        self.assertEqual(codec.samples_per_frame, 160)


class G711ReferenceTest(TestCase):
    """The table-driven codecs must stay bit-exact vs the per-sample transforms."""

    def test_ulaw_encode_exhaustive(self) -> None:
        pcm = struct.pack("<65536h", *range(-32768, 32768))
        expected = bytes(_ulaw_encode_sample(s) for s in range(-32768, 32768))
        self.assertEqual(PcmuCodec().encode(pcm), expected)

    def test_ulaw_decode_exhaustive(self) -> None:
        payload = bytes(range(256))
        expected = struct.pack("<256h", *(_ulaw_decode_sample(b) for b in range(256)))
        self.assertEqual(PcmuCodec().decode(payload), expected)

    def test_alaw_encode_exhaustive(self) -> None:
        pcm = struct.pack("<65536h", *range(-32768, 32768))
        expected = bytes(_alaw_encode_sample(s) for s in range(-32768, 32768))
        self.assertEqual(PcmaCodec().encode(pcm), expected)

    def test_alaw_decode_exhaustive(self) -> None:
        payload = bytes(range(256))
        expected = struct.pack("<256h", *(_alaw_decode_sample(b) for b in range(256)))
        self.assertEqual(PcmaCodec().decode(payload), expected)

    def test_encode_empty(self) -> None:
        self.assertEqual(PcmuCodec().encode(b""), b"")
        self.assertEqual(PcmaCodec().encode(b""), b"")

    def test_decode_empty(self) -> None:
        self.assertEqual(PcmuCodec().decode(b""), b"")
        self.assertEqual(PcmaCodec().decode(b""), b"")

    def test_encode_truncates_trailing_odd_byte(self) -> None:
        pcm = struct.pack("<2h", 1000, -1000)
        self.assertEqual(PcmuCodec().encode(pcm + b"\x7f"), PcmuCodec().encode(pcm))


class L16CodecTest(TestCase):
    def test_roundtrip(self) -> None:
        codec = L16Codec()
        pcm = b""
        for i in range(160):
            pcm += struct.pack("<h", i * 100 - 8000)

        encoded = codec.encode(pcm)
        self.assertEqual(len(encoded), 320)

        decoded = codec.decode(encoded)
        self.assertEqual(decoded, pcm)

    def test_byte_order(self) -> None:
        codec = L16Codec()
        self.assertEqual(codec.encode(b"\x01\x02\x03\x04"), b"\x02\x01\x04\x03")
        self.assertEqual(codec.decode(b"\x02\x01\x04\x03"), b"\x01\x02\x03\x04")

    def test_empty(self) -> None:
        codec = L16Codec()
        self.assertEqual(codec.encode(b""), b"")
        self.assertEqual(codec.decode(b""), b"")

    def test_odd_length_rejected(self) -> None:
        with self.assertRaises(ValueError):
            L16Codec().encode(b"\x01\x02\x03")

    def test_properties(self) -> None:
        codec = L16Codec()
        self.assertEqual(codec.name, "L16")
        self.assertEqual(codec.sample_rate, 8000)
        self.assertEqual(codec.samples_per_frame, 160)


@unittest.skipUnless(_HAS_OPUS, "opuslib/libopus not available")
class OpusPlcTest(TestCase):
    def test_conceal_native(self) -> None:
        codec = OpusCodec(sample_rate=48000, channels=1)
        pcm = struct.pack("<960h", *([1000] * 960))
        codec.decode(codec.encode(pcm))  # warm decoder state
        concealed = codec.conceal(960)
        self.assertEqual(len(concealed), 1920)  # one 20 ms frame, s16le mono

    def test_conceal_rounds_to_whole_frames(self) -> None:
        codec = OpusCodec(sample_rate=48000, channels=1)
        codec.decode(codec.encode(b"\x00\x00" * 960))
        self.assertEqual(len(codec.conceal(1920)), 2 * 1920)


class RegistryTest(TestCase):
    def test_get_pcmu(self) -> None:
        codec = get_codec(PayloadType.PCMU)
        self.assertIsInstance(codec, PcmuCodec)

    def test_get_pcma(self) -> None:
        codec = get_codec(PayloadType.PCMA)
        self.assertIsInstance(codec, PcmaCodec)

    def test_get_l16(self) -> None:
        codec = get_codec(PayloadType.L16)
        self.assertIsInstance(codec, L16Codec)

    def test_get_unknown(self) -> None:
        with self.assertRaises(ValueError):
            get_codec(99)
