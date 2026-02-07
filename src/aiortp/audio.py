from dataclasses import dataclass


@dataclass
class AudioFrame:
    data: bytes
    timestamp: int
    sample_rate: int = 8000
    samples: int = 160
