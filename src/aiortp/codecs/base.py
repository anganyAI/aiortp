from abc import ABC, abstractmethod


class Codec(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def sample_rate(self) -> int: ...

    @property
    @abstractmethod
    def samples_per_frame(self) -> int: ...

    @abstractmethod
    def encode(self, pcm: bytes) -> bytes:
        """Encode 16-bit signed LE PCM to codec payload."""
        ...

    @abstractmethod
    def decode(self, payload: bytes) -> bytes:
        """Decode codec payload to 16-bit signed LE PCM."""
        ...

    def conceal(self, num_samples: int) -> bytes | None:
        """Generate concealment PCM for *num_samples* lost samples.

        Returns 16-bit signed LE PCM, or ``None`` when the codec has no
        native concealment (callers fall back to a generic concealer).
        """
        return None
