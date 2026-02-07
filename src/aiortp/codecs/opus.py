"""Optional Opus codec wrapper. Requires the `opuslib` package."""

from .base import Codec

try:
    import opuslib  # type: ignore[import-untyped]

    _HAS_OPUS = True
except ImportError:
    _HAS_OPUS = False


class OpusCodec(Codec):
    def __init__(
        self, sample_rate: int = 48000, channels: int = 1, frame_ms: int = 20
    ) -> None:
        if not _HAS_OPUS:
            raise ImportError(
                "opuslib is required for Opus support. "
                "Install with: pip install aiortp[opus]"
            )
        self._sample_rate = sample_rate
        self._channels = channels
        self._frame_ms = frame_ms
        self._samples_per_frame = sample_rate * frame_ms // 1000
        self._encoder = opuslib.Encoder(sample_rate, channels, opuslib.APPLICATION_VOIP)
        self._decoder = opuslib.Decoder(sample_rate, channels)

    @property
    def name(self) -> str:
        return "opus"

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    @property
    def samples_per_frame(self) -> int:
        return self._samples_per_frame

    def encode(self, pcm: bytes) -> bytes:
        """Encode s16le PCM to Opus."""
        return self._encoder.encode(pcm, self._samples_per_frame)

    def decode(self, payload: bytes) -> bytes:
        """Decode Opus to s16le PCM."""
        return self._decoder.decode(payload, self._samples_per_frame)
