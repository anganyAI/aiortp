from unittest import TestCase

from aiortp.dtmf import DTMF_EVENTS, DtmfEvent, DtmfReceiver, DtmfSender
from aiortp.packet import RtpPacket
from aiortp.sender import RtpSender
from aiortp.transport import RtpTransport


class DtmfEventTest(TestCase):
    def test_serialize_parse_roundtrip(self) -> None:
        event = DtmfEvent(event=1, end=False, volume=10, duration=400)
        data = event.serialize()
        self.assertEqual(len(data), 4)

        parsed = DtmfEvent.parse(data)
        self.assertEqual(parsed.event, 1)
        self.assertFalse(parsed.end)
        self.assertEqual(parsed.volume, 10)
        self.assertEqual(parsed.duration, 400)

    def test_end_flag(self) -> None:
        event = DtmfEvent(event=5, end=True, volume=10, duration=1280)
        data = event.serialize()
        parsed = DtmfEvent.parse(data)
        self.assertTrue(parsed.end)
        self.assertEqual(parsed.event, 5)
        self.assertEqual(parsed.duration, 1280)

    def test_digit_property(self) -> None:
        for digit, code in DTMF_EVENTS.items():
            event = DtmfEvent(event=code, end=False, volume=10, duration=100)
            self.assertEqual(event.digit, digit)

    def test_parse_too_short(self) -> None:
        with self.assertRaises(ValueError):
            DtmfEvent.parse(b"\x00\x00")


class DtmfReceiverTest(TestCase):
    def test_receive_digit(self) -> None:
        received: list[tuple[str, int]] = []

        def on_dtmf(digit: str, duration: int) -> None:
            received.append((digit, duration))

        receiver = DtmfReceiver(on_dtmf)

        # Progress packet
        ev = DtmfEvent(event=1, end=False, volume=10, duration=400)
        pkt = RtpPacket(
            payload_type=101, sequence_number=100, timestamp=1000, payload=ev.serialize()
        )
        receiver.handle_packet(pkt)
        self.assertEqual(len(received), 0)

        # End packet
        ev = DtmfEvent(event=1, end=True, volume=10, duration=1280)
        pkt = RtpPacket(
            payload_type=101,
            sequence_number=101,
            timestamp=1000,
            payload=ev.serialize(),
        )
        receiver.handle_packet(pkt)
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0], ("1", 1280))

    def test_no_duplicate_end(self) -> None:
        """Redundant end packets should not trigger multiple callbacks."""
        received: list[tuple[str, int]] = []

        def on_dtmf(digit: str, duration: int) -> None:
            received.append((digit, duration))

        receiver = DtmfReceiver(on_dtmf)

        # Send 3 redundant end packets (same timestamp)
        for seq in range(3):
            ev = DtmfEvent(event=5, end=True, volume=10, duration=1280)
            pkt = RtpPacket(
                payload_type=101,
                sequence_number=100 + seq,
                timestamp=2000,
                payload=ev.serialize(),
            )
            receiver.handle_packet(pkt)

        # Should only get one callback
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0], ("5", 1280))

    def test_two_digits(self) -> None:
        """Two different digits (different timestamps) trigger two callbacks."""
        received: list[tuple[str, int]] = []

        def on_dtmf(digit: str, duration: int) -> None:
            received.append((digit, duration))

        receiver = DtmfReceiver(on_dtmf)

        # First digit
        ev = DtmfEvent(event=1, end=True, volume=10, duration=1280)
        pkt = RtpPacket(
            payload_type=101, sequence_number=100, timestamp=1000, payload=ev.serialize()
        )
        receiver.handle_packet(pkt)

        # Second digit (different timestamp)
        ev = DtmfEvent(event=2, end=True, volume=10, duration=1280)
        pkt = RtpPacket(
            payload_type=101, sequence_number=101, timestamp=2000, payload=ev.serialize()
        )
        receiver.handle_packet(pkt)

        self.assertEqual(len(received), 2)
        self.assertEqual(received[0][0], "1")
        self.assertEqual(received[1][0], "2")


class DtmfSenderTest(TestCase):
    def test_send_digit(self) -> None:
        """Sender generates progress + 3 end packets."""
        sent_data: list[bytes] = []

        transport = RtpTransport(on_rtp=lambda d: None, on_rtcp=lambda d: None)
        # Mock the transport send
        transport.send = lambda data, addr=None: sent_data.append(data)  # type: ignore[assignment]

        sender = RtpSender(transport=transport, payload_type=0, ssrc=12345)
        dtmf_sender = DtmfSender(sender=sender, dtmf_payload_type=101)

        packets = dtmf_sender.send_digit("1", duration_ms=160)

        # Should have progress packets + 3 end packets
        # 160ms / 20ms = 8 steps, but progress packets are for durations < total
        # So we get 7 progress packets (at 160, 320, ..., 1120) + 3 end packets = 10
        self.assertTrue(len(packets) >= 3)  # At least the 3 end packets

        # Verify last 3 packets are end packets
        for pkt_data in packets[-3:]:
            pkt = RtpPacket.parse(pkt_data)
            ev = DtmfEvent.parse(pkt.payload)
            self.assertTrue(ev.end)
            self.assertEqual(ev.event, 1)  # digit "1"
