"""Optional G.722 wideband codec wrapper.

Requires the ``G722`` package from PyPI::

    pip install aiortp[g722]

G.722 provides 7 kHz wideband audio at 64 kbps (16 kHz sample rate).
Note the historical RFC 3551 quirk: the RTP clock rate is 8000 Hz even
though the actual audio is sampled at 16000 Hz.
"""

import sys
from array import array

from .base import Codec

try:
    from G722 import G722 as _G722Engine  # type: ignore[import-untyped]

    _HAS_G722 = True
except ImportError:
    _HAS_G722 = False

_IS_BIG_ENDIAN = sys.byteorder == "big"


class G722Codec(Codec):
    """G.722 wideband codec (16 kHz audio, 64 kbps).

    Wraps the ``G722`` C extension.  Each 20 ms frame contains 320 samples
    at 16 kHz, encoded into 160 bytes.
    """

    def __init__(self) -> None:
        if not _HAS_G722:
            raise ImportError(
                "G722 is required for G.722 support. Install with: pip install aiortp[g722]"
            )
        self._encoder = _G722Engine(16000, 64000)
        self._decoder = _G722Engine(16000, 64000)

    @property
    def name(self) -> str:
        return "G722"

    @property
    def sample_rate(self) -> int:
        return 16000

    @property
    def samples_per_frame(self) -> int:
        return 320  # 20ms at 16kHz

    def encode(self, pcm: bytes) -> bytes:
        """Encode s16le PCM to G.722.

        Args:
            pcm: Raw PCM-16 LE audio bytes (320 samples = 640 bytes per frame).

        Returns:
            G.722 encoded bytes (160 bytes per frame).
        """
        # The C extension consumes any native-order 16-bit buffer directly —
        # no per-sample boxing on the Python side.
        samples = array("h")
        samples.frombytes(pcm[: len(pcm) // 2 * 2])
        if _IS_BIG_ENDIAN:
            samples.byteswap()
        encoded: bytes = self._encoder.encode(samples)
        return encoded

    def decode(self, payload: bytes) -> bytes:
        """Decode G.722 payload to s16le PCM.

        Args:
            payload: G.722 encoded bytes (160 bytes per frame).

        Returns:
            Raw PCM-16 LE audio bytes (320 samples = 640 bytes per frame).
        """
        # The C extension returns an array("h") of native-order samples;
        # tobytes() emits them in a single C-speed pass.
        decoded = self._decoder.decode(payload)
        if _IS_BIG_ENDIAN:
            decoded.byteswap()
        pcm: bytes = decoded.tobytes()
        return pcm
