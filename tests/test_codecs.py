import struct
from unittest import TestCase

from aiortp.codecs import PayloadType, get_codec
from aiortp.codecs.g711 import PcmaCodec, PcmuCodec
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

    def test_properties(self) -> None:
        codec = L16Codec()
        self.assertEqual(codec.name, "L16")
        self.assertEqual(codec.sample_rate, 8000)
        self.assertEqual(codec.samples_per_frame, 160)


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
