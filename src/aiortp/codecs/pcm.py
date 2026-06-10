"""L16 (Linear 16-bit PCM) codec — s16le ↔ s16be (network byte order) conversion."""

from array import array

from .base import Codec


def _byteswap16(data: bytes) -> bytes:
    """Swap the byte order of each 16-bit sample (one C-speed pass)."""
    samples = array("h")
    samples.frombytes(data)
    samples.byteswap()
    return samples.tobytes()


class L16Codec(Codec):
    @property
    def name(self) -> str:
        return "L16"

    @property
    def sample_rate(self) -> int:
        return 8000

    @property
    def samples_per_frame(self) -> int:
        return 160

    def encode(self, pcm: bytes) -> bytes:
        """Convert s16le PCM to s16be (network byte order)."""
        return _byteswap16(pcm)

    def decode(self, payload: bytes) -> bytes:
        """Convert s16be (network byte order) to s16le PCM."""
        return _byteswap16(payload)
