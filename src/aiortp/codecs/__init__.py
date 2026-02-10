"""Codec registry for RTP payload types."""

from enum import IntEnum

from .base import Codec
from .g711 import PcmaCodec, PcmuCodec
from .pcm import L16Codec


class PayloadType(IntEnum):
    PCMU = 0
    G722 = 9
    PCMA = 8
    L16 = 11


_registry: dict[int, type[Codec]] = {}


def register_codec(pt: int, cls: type[Codec]) -> None:
    """Register a codec class for a payload type."""
    _registry[pt] = cls


def get_codec(pt: int) -> Codec:
    """Get a codec instance for a payload type."""
    cls = _registry.get(pt)
    if cls is None:
        raise ValueError(f"No codec registered for payload type {pt}")
    return cls()


# Auto-register built-in codecs
register_codec(PayloadType.PCMU, PcmuCodec)
register_codec(PayloadType.PCMA, PcmaCodec)
register_codec(PayloadType.L16, L16Codec)

# Try to register G.722 if available
try:
    from .g722 import G722Codec

    register_codec(PayloadType.G722, G722Codec)
except ImportError:
    pass

# Try to register Opus if available
try:
    from .opus import OpusCodec

    register_codec(111, OpusCodec)
except ImportError:
    pass

__all__ = [
    "Codec",
    "G722Codec",
    "PayloadType",
    "PcmuCodec",
    "PcmaCodec",
    "L16Codec",
    "get_codec",
    "register_codec",
]
