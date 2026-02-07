from .packet import RtpPacket
from .transport import RtpTransport
from .utils import random16, random32, uint16_add


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

    def send_frame(
        self,
        payload: bytes,
        timestamp: int,
        marker: int = 0,
        addr: tuple[str, int] | None = None,
    ) -> None:
        packet = RtpPacket(
            payload_type=self._payload_type,
            marker=marker,
            sequence_number=self._sequence_number,
            timestamp=timestamp,
            ssrc=self._ssrc,
            payload=payload,
        )
        data = packet.serialize()
        self._transport.send(data, addr)
        self._sequence_number = uint16_add(self._sequence_number, 1)
        self._packets_sent += 1
        self._octets_sent += len(payload)
