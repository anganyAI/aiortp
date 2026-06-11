"""RFC 3389 comfort noise: payload codec, level measurement, generation.

The CN payload carries the noise level as a single byte in -dBov
(0 = overload point, 127 = silence); optional spectral coefficients
may follow and are ignored here.
"""

from __future__ import annotations

import math
import random
from array import array

# -dBov sent when no level has been measured yet (quiet line noise)
DEFAULT_CN_LEVEL = 70

_SILENCE_LEVEL = 127
_FULL_SCALE = 32767.0


def parse_cn_payload(payload: bytes) -> int:
    """Return the noise level in -dBov; spectral coefficients are ignored."""
    if not payload:
        return _SILENCE_LEVEL
    return payload[0] & 0x7F


def build_cn_payload(level: int) -> bytes:
    return bytes([max(0, min(_SILENCE_LEVEL, level))])


def measure_level(pcm: bytes) -> int:
    """Measure s16le PCM energy as an RFC 3389 level (-dBov)."""
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if not samples:
        return _SILENCE_LEVEL
    mean_square = sum(s * s for s in samples) / len(samples)
    if mean_square < 1.0:
        return _SILENCE_LEVEL
    dbov = -10.0 * math.log10(mean_square / (_FULL_SCALE * _FULL_SCALE))
    return max(0, min(_SILENCE_LEVEL, round(dbov)))


class NoiseGenerator:
    """White-noise PCM at a fixed -dBov level.

    Uniform noise has an RMS of peak/sqrt(3); the amplitude compensates
    for it so the generated signal measures at the requested level.
    """

    def __init__(self, level: int) -> None:
        self.level = level
        self._scale = min(_FULL_SCALE, _FULL_SCALE * 10.0 ** (-level / 20.0) * math.sqrt(3.0))
        self._random = random.Random()  # noqa: S311  # nosec B311 — noise, not crypto

    def generate(self, num_samples: int) -> bytes:
        rand = self._random.random
        scale = self._scale
        out = array("h", (int(scale * (rand() * 2.0 - 1.0)) for _ in range(num_samples)))
        return out.tobytes()
