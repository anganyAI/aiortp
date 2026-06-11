"""Tests for RFC 3389 comfort noise primitives."""

import math
import struct

from aiortp.cn import NoiseGenerator, build_cn_payload, measure_level, parse_cn_payload


def test_payload_roundtrip() -> None:
    assert parse_cn_payload(build_cn_payload(70)) == 70
    assert parse_cn_payload(build_cn_payload(0)) == 0
    assert build_cn_payload(200) == bytes([127])  # clamped


def test_parse_ignores_spectral_coefficients() -> None:
    assert parse_cn_payload(bytes([55, 0x12, 0x34, 0x56])) == 55


def test_parse_empty_payload_is_silence() -> None:
    assert parse_cn_payload(b"") == 127


def test_measure_level_of_known_sine() -> None:
    """A full-scale sine has an RMS of peak/sqrt(2) -> ~3 dBov below overload."""
    samples = [int(32767 * math.sin(2 * math.pi * 440 * i / 8000)) for i in range(800)]
    level = measure_level(struct.pack("<800h", *samples))
    assert level == 3

    quarter = [s // 4 for s in samples]  # -12 dB on top
    assert measure_level(struct.pack("<800h", *quarter)) == 15


def test_measure_level_of_silence() -> None:
    assert measure_level(b"\x00\x00" * 160) == 127
    assert measure_level(b"") == 127


def test_noise_generator_matches_requested_level() -> None:
    """Generated noise measures back at the requested -dBov (sqrt(3) compensation)."""
    for level in (20, 40, 70):
        pcm = NoiseGenerator(level).generate(8000)
        assert abs(measure_level(pcm) - level) <= 1


def test_noise_generator_output_shape() -> None:
    gen = NoiseGenerator(70)
    pcm = gen.generate(160)
    assert len(pcm) == 320  # s16le
    assert pcm != b"\x00" * 320  # actual noise, not silence


def test_noise_generator_near_silence_level() -> None:
    pcm = NoiseGenerator(127).generate(1600)
    assert measure_level(pcm) >= 120  # essentially silence
