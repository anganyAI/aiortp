import logging

from .packet import RTP_HISTORY_SIZE, RtpPacket
from .transport import RtpTransport
from .utils import random16, random32, uint16_add, uint32_add

logger = logging.getLogger(__name__)

# Number of packets to keep for NACK retransmission
_HISTORY_CAPACITY = RTP_HISTORY_SIZE


class RtpSender:
    def __init__(
        self,
        transport: RtpTransport,
        payload_type: int,
        ssrc: int | None = None,
        clock_rate: int = 8000,
    ) -> None:
        self._transport = transport
        self._payload_type = payload_type
        self._ssrc = ssrc if ssrc is not None else random32()
        self._clock_rate = clock_rate
        self._sequence_number = random16()
        self._packets_sent = 0
        self._octets_sent = 0
        self._last_rtp_timestamp = 0

        # Packet history for NACK retransmission (seq -> serialized bytes)
        self._history: dict[int, bytes] = {}

        # Auto-timestamp state
        self._current_timestamp = random32()
        self._timestamp_increment = 0

    @property
    def ssrc(self) -> int:
        return self._ssrc

    @property
    def packets_sent(self) -> int:
        return self._packets_sent

    @property
    def octets_sent(self) -> int:
        return self._octets_sent

    @property
    def sequence_number(self) -> int:
        return self._sequence_number

    @property
    def last_rtp_timestamp(self) -> int:
        return self._last_rtp_timestamp

    @property
    def current_timestamp(self) -> int:
        return self._current_timestamp

    @property
    def timestamp_increment(self) -> int:
        return self._timestamp_increment

    @timestamp_increment.setter
    def timestamp_increment(self, value: int) -> None:
        self._timestamp_increment = value

    def advance_timestamp(self) -> None:
        """Advance the auto-timestamp by one increment."""
        self._current_timestamp = uint32_add(self._current_timestamp, self._timestamp_increment)

    def send_raw(
        self,
        payload_type: int,
        payload: bytes,
        timestamp: int,
        marker: int = 0,
        addr: tuple[str, int] | None = None,
    ) -> None:
        """Send a packet with an arbitrary payload type.

        Used by DtmfSender and other subsystems that need to send
        with a payload type different from the session default.
        """
        packet = RtpPacket(
            payload_type=payload_type,
            marker=marker,
            sequence_number=self._sequence_number,
            timestamp=timestamp,
            ssrc=self._ssrc,
            payload=payload,
        )
        data = packet.serialize()
        self._transport.send(data, addr)

        # Store in history for NACK retransmission
        seq = self._sequence_number
        self._history[seq] = data
        self._evict_old_history(seq)

        self._sequence_number = uint16_add(self._sequence_number, 1)
        self._packets_sent += 1
        self._octets_sent += len(payload)
        self._last_rtp_timestamp = timestamp

    def _evict_old_history(self, current_seq: int) -> None:
        """Remove history entries older than _HISTORY_CAPACITY."""
        if len(self._history) <= _HISTORY_CAPACITY:
            return
        min_seq = uint16_add(current_seq, -_HISTORY_CAPACITY)
        # Remove entries outside the valid window
        for seq in list(self._history):
            # Check if seq is before min_seq (with wraparound)
            diff = (min_seq - seq) & 0xFFFF
            if diff < 0x8000 and diff > 0:
                del self._history[seq]

    def retransmit(
        self,
        seq_numbers: list[int],
        addr: tuple[str, int] | None = None,
    ) -> int:
        """Retransmit packets requested by NACK.

        Returns the number of packets actually retransmitted.
        """
        count = 0
        for seq in seq_numbers:
            data = self._history.get(seq)
            if data is not None:
                self._transport.send(data, addr)
                count += 1
        if count > 0:
            logger.debug("Retransmitted %d/%d NACKed packets", count, len(seq_numbers))
        return count

    def send_frame(
        self,
        payload: bytes,
        timestamp: int,
        marker: int = 0,
        addr: tuple[str, int] | None = None,
    ) -> None:
        self.send_raw(self._payload_type, payload, timestamp, marker, addr)

    def send_frame_auto(
        self,
        payload: bytes,
        marker: int = 0,
        addr: tuple[str, int] | None = None,
    ) -> int:
        """Send a frame using the auto-incrementing timestamp.

        Returns the RTP timestamp used for this frame.
        """
        ts = self._current_timestamp
        self.send_frame(payload, ts, marker, addr)
        self.advance_timestamp()
        return ts
