"""
Codec roundtrip example: encode and decode audio with each built-in codec.

Demonstrates:
  - Using the codec registry (get_codec / PayloadType)
  - Encoding s16le PCM → codec payload → decoding back to PCM
  - Measuring roundtrip error for lossy codecs (G.711)
"""

import math
import struct

from aiortp import PayloadType, get_codec


def generate_sine_pcm(frequency: float, sample_rate: int, num_samples: int) -> bytes:
    """Generate a sine wave as s16le PCM."""
    buf = bytearray()
    for i in range(num_samples):
        sample = int(16000 * math.sin(2 * math.pi * frequency * i / sample_rate))
        buf += struct.pack("<h", sample)
    return bytes(buf)


def rms_error(original: bytes, decoded: bytes) -> float:
    """Compute RMS error between two s16le PCM buffers."""
    n = len(original) // 2
    total = 0.0
    for i in range(n):
        a = struct.unpack_from("<h", original, i * 2)[0]
        b = struct.unpack_from("<h", decoded, i * 2)[0]
        total += (a - b) ** 2
    return math.sqrt(total / n)


def main() -> None:
    pcm = generate_sine_pcm(frequency=440.0, sample_rate=8000, num_samples=160)
    print(f"Input: 160 samples of 440 Hz sine, {len(pcm)} bytes s16le PCM\n")

    for pt in (PayloadType.PCMU, PayloadType.PCMA, PayloadType.L16):
        codec = get_codec(pt)
        encoded = codec.encode(pcm)
        decoded = codec.decode(encoded)
        error = rms_error(pcm, decoded)

        print(f"{codec.name} (PT={int(pt)}):")
        print(f"  Encoded size: {len(encoded)} bytes")
        print(f"  Decoded size: {len(decoded)} bytes")
        print(f"  RMS error:    {error:.1f}")
        print(f"  Lossless:     {pcm == decoded}")
        print()


if __name__ == "__main__":
    main()
