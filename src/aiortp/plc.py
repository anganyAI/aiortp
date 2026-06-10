"""Generic PCM packet loss concealment.

Repeats the last received frame with progressive attenuation, then emits
silence once the fade budget is exhausted.  Keeps the delivered PCM
stream temporally continuous for codecs without native concealment.
"""

from array import array

DEFAULT_FADE_MS = 60


class PcmConcealer:
    """Synthesizes replacement PCM for confirmed-lost audio frames."""

    def __init__(self, sample_rate: int, fade_ms: int = DEFAULT_FADE_MS) -> None:
        self._fade_samples = sample_rate * fade_ms // 1000
        self._history: array[int] | None = None
        self._elapsed = 0  # samples concealed since the last real frame

    @property
    def frame_samples(self) -> int:
        """Sample count of the last recorded frame (0 before any frame)."""
        return len(self._history) if self._history is not None else 0

    def update(self, pcm: bytes) -> None:
        """Record a received frame as concealment source; resets the fade."""
        history = array("h")
        history.frombytes(pcm)
        self._history = history
        self._elapsed = 0

    def conceal(self, num_samples: int) -> bytes:
        """Generate *num_samples* of s16le concealment PCM.

        The last frame is repeated with a linear fade to zero across the
        fade budget; samples beyond the budget are silence, preserving
        timeline alignment on long bursts without synthetic artifacts.
        """
        out = array("h", bytes(2 * num_samples))
        source = self._history
        if source:
            fade = self._fade_samples
            faded = min(num_samples, max(fade - self._elapsed, 0))
            for i in range(faded):
                position = self._elapsed + i
                out[i] = source[position % len(source)] * (fade - position) // fade
        self._elapsed += num_samples
        return out.tobytes()
