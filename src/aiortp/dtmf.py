"""RFC 2833/4733 DTMF telephone-event handling."""

import struct
from collections.abc import Callable
from dataclasses import dataclass
from typing import Optional

from .packet import RtpPacket
from .sender import RtpSender
from .utils import uint16_add

# DTMF digit to event code mapping
DTMF_EVENTS: dict[str, int] = {
    "0": 0,
    "1": 1,
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
    "8": 8,
    "9": 9,
    "*": 10,
    "#": 11,
    "A": 12,
    "B": 13,
    "C": 14,
    "D": 15,
}

EVENT_TO_DIGIT: dict[int, str] = {v: k for k, v in DTMF_EVENTS.items()}


@dataclass
class DtmfEvent:
    """RFC 4733 telephone-event payload (4 bytes)."""

    event: int
    end: bool
    volume: int
    duration: int

    def serialize(self) -> bytes:
        flags = (0x80 if self.end else 0x00) | (self.volume & 0x3F)
        return struct.pack("!BBH", self.event, flags, self.duration)

    @classmethod
    def parse(cls, data: bytes) -> "DtmfEvent":
        if len(data) < 4:
            raise ValueError("DTMF event payload must be at least 4 bytes")
        event, flags, duration = struct.unpack("!BBH", data[:4])
        end = bool(flags & 0x80)
        volume = flags & 0x3F
        return cls(event=event, end=end, volume=volume, duration=duration)

    @property
    def digit(self) -> str:
        return EVENT_TO_DIGIT.get(self.event, "?")


class DtmfReceiver:
    """Tracks DTMF digit state from incoming RTP telephone-event packets."""

    def __init__(self, on_dtmf: Callable[[str, int], None]) -> None:
        self._on_dtmf = on_dtmf
        self._current_event: Optional[int] = None
        self._current_timestamp: Optional[int] = None
        self._end_seen = False

    def handle_packet(self, packet: RtpPacket) -> None:
        event = DtmfEvent.parse(packet.payload)

        # New event (different timestamp means new digit)
        if packet.timestamp != self._current_timestamp:
            self._current_event = event.event
            self._current_timestamp = packet.timestamp
            self._end_seen = False

        if event.end and not self._end_seen:
            self._end_seen = True
            self._on_dtmf(event.digit, event.duration)


class DtmfSender:
    """Generates DTMF telephone-event RTP packets."""

    def __init__(
        self,
        sender: RtpSender,
        dtmf_payload_type: int = 101,
        clock_rate: int = 8000,
    ) -> None:
        self._sender = sender
        self._dtmf_payload_type = dtmf_payload_type
        self._clock_rate = clock_rate

    def send_digit(
        self,
        digit: str,
        duration_ms: int = 160,
        volume: int = 10,
        timestamp: int = 0,
        addr: tuple[str, int] | None = None,
    ) -> list[bytes]:
        """
        Generate DTMF packets for a digit.
        Returns list of serialized packets (for testing).

        Sends progress packets every 20ms, then 3 redundant end packets.
        """
        event_code = DTMF_EVENTS.get(digit.upper())
        if event_code is None:
            raise ValueError(f"Invalid DTMF digit: {digit}")

        duration_samples = (duration_ms * self._clock_rate) // 1000
        step_samples = (20 * self._clock_rate) // 1000  # 20ms steps

        # All packets for this event share the same RTP timestamp
        event_timestamp = timestamp

        packets: list[bytes] = []

        # Progress packets
        current_duration = step_samples
        while current_duration < duration_samples:
            ev = DtmfEvent(
                event=event_code,
                end=False,
                volume=volume,
                duration=current_duration,
            )
            pkt = RtpPacket(
                payload_type=self._dtmf_payload_type,
                sequence_number=self._sender.sequence_number,
                timestamp=event_timestamp,
                ssrc=self._sender.ssrc,
                payload=ev.serialize(),
            )
            data = pkt.serialize()
            self._sender._transport.send(data, addr)
            self._sender._sequence_number = uint16_add(
                self._sender._sequence_number, 1
            )
            self._sender._packets_sent += 1
            self._sender._octets_sent += len(ev.serialize())
            packets.append(data)
            current_duration += step_samples

        # End packets (3 redundant, RFC 4733 §2.5.1.4)
        for i in range(3):
            ev = DtmfEvent(
                event=event_code,
                end=True,
                volume=volume,
                duration=duration_samples,
            )
            pkt = RtpPacket(
                payload_type=self._dtmf_payload_type,
                marker=1 if i == 0 else 0,
                sequence_number=self._sender.sequence_number,
                timestamp=event_timestamp,
                ssrc=self._sender.ssrc,
                payload=ev.serialize(),
            )
            data = pkt.serialize()
            self._sender._transport.send(data, addr)
            self._sender._sequence_number = uint16_add(
                self._sender._sequence_number, 1
            )
            self._sender._packets_sent += 1
            self._sender._octets_sent += len(ev.serialize())
            packets.append(data)

        return packets
