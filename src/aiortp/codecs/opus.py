"""Optional Opus codec wrapper. Requires the `opuslib` package."""

from .base import Codec

try:
    import opuslib  # type: ignore[import-untyped]

    # Low-level APIs: decoder for native PLC, encoder ctl for DTX
    # (opuslib's high-level _set_dtx wrapper is broken — it sends the
    # get request)
    import opuslib.api.ctl  # type: ignore[import-untyped]
    import opuslib.api.decoder  # type: ignore[import-untyped]
    import opuslib.api.encoder  # type: ignore[import-untyped]

    _HAS_OPUS = True
except Exception:  # noqa: BLE001 — opuslib raises a plain Exception when libopus is missing
    _HAS_OPUS = False


class OpusCodec(Codec):
    def __init__(
        self,
        sample_rate: int = 48000,
        channels: int = 1,
        frame_ms: int = 20,
        dtx: bool = False,
    ) -> None:
        if not _HAS_OPUS:
            raise ImportError(
                "opuslib is required for Opus support. Install with: pip install aiortp[opus]"
            )
        self._sample_rate = sample_rate
        self._channels = channels
        self._frame_ms = frame_ms
        self._samples_per_frame = sample_rate * frame_ms // 1000
        self._encoder = opuslib.Encoder(sample_rate, channels, opuslib.APPLICATION_VOIP)
        self._decoder = opuslib.Decoder(sample_rate, channels)
        if dtx:
            # Whether libopus actually emits DTX frames depends on its
            # mode selection (SILK) and build; transmission suppression
            # in aiortp is handled by paced mode + RFC 3389 CN regardless.
            # Caveat: opus_encoder_ctl is variadic and ctypes cannot make
            # variadic calls reliably on Apple Silicon — the flag may not
            # reach the encoder there.
            opuslib.api.encoder.encoder_ctl(self._encoder.encoder_state, opuslib.api.ctl.set_dtx, 1)

    @property
    def dtx(self) -> bool:
        """Whether the encoder's DTX flag is set."""
        return bool(
            opuslib.api.encoder.encoder_ctl(self._encoder.encoder_state, opuslib.api.ctl.get_dtx)
        )

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

    def conceal(self, num_samples: int) -> bytes:
        """Generate concealment PCM using native libopus PLC.

        libopus synthesizes one frame per NULL-payload decode call, so the
        requested duration is rounded to whole frames.  The high-level
        ``opuslib.Decoder`` rejects ``None`` payloads, hence the low-level
        API call.
        """
        frames = max(1, round(num_samples / self._samples_per_frame))
        return b"".join(
            opuslib.api.decoder.decode(
                self._decoder.decoder_state,
                None,
                0,
                self._samples_per_frame,
                False,
                self._channels,
            )
            for _ in range(frames)
        )
