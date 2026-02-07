"""L16 (Linear 16-bit PCM) codec — s16le ↔ s16be (network byte order) conversion."""

import struct

from .base import Codec


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
        n_samples = len(pcm) // 2
        samples = struct.unpack(f"<{n_samples}h", pcm)
        return struct.pack(f">{n_samples}h", *samples)

    def decode(self, payload: bytes) -> bytes:
        """Convert s16be (network byte order) to s16le PCM."""
        n_samples = len(payload) // 2
        samples = struct.unpack(f">{n_samples}h", payload)
        return struct.pack(f"<{n_samples}h", *samples)
