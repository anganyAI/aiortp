from unittest import TestCase

from aiortp.packet import RtpPacket
from aiortp.stats import NackGenerator, StreamStatistics


class NackGeneratorTest(TestCase):
    def test_sequential(self) -> None:
        nack = NackGenerator()
        self.assertFalse(nack.add(RtpPacket(sequence_number=0)))
        self.assertFalse(nack.add(RtpPacket(sequence_number=1)))
        self.assertFalse(nack.add(RtpPacket(sequence_number=2)))
        self.assertEqual(nack.missing, set())

    def test_gap(self) -> None:
        nack = NackGenerator()
        self.assertFalse(nack.add(RtpPacket(sequence_number=0)))
        self.assertTrue(nack.add(RtpPacket(sequence_number=2)))
        self.assertEqual(nack.missing, {1})

    def test_gap_then_fill(self) -> None:
        nack = NackGenerator()
        self.assertFalse(nack.add(RtpPacket(sequence_number=0)))
        self.assertTrue(nack.add(RtpPacket(sequence_number=3)))
        self.assertEqual(nack.missing, {1, 2})
        self.assertFalse(nack.add(RtpPacket(sequence_number=1)))
        self.assertEqual(nack.missing, {2})

    def test_first_packet(self) -> None:
        nack = NackGenerator()
        self.assertFalse(nack.add(RtpPacket(sequence_number=100)))
        self.assertEqual(nack.max_seq, 100)
        self.assertEqual(nack.missing, set())


class StreamStatisticsTest(TestCase):
    def test_sequential(self) -> None:
        stats = StreamStatistics(clockrate=8000)
        stats.add(RtpPacket(sequence_number=0, timestamp=0))
        stats.add(RtpPacket(sequence_number=1, timestamp=160))
        stats.add(RtpPacket(sequence_number=2, timestamp=320))

        self.assertEqual(stats.packets_received, 3)
        self.assertEqual(stats.packets_expected, 3)
        self.assertEqual(stats.packets_lost, 0)
        self.assertEqual(stats.base_seq, 0)
        self.assertEqual(stats.max_seq, 2)

    def test_with_loss(self) -> None:
        stats = StreamStatistics(clockrate=8000)
        stats.add(RtpPacket(sequence_number=0, timestamp=0))
        stats.add(RtpPacket(sequence_number=1, timestamp=160))
        # skip seq 2
        stats.add(RtpPacket(sequence_number=3, timestamp=480))

        self.assertEqual(stats.packets_received, 3)
        self.assertEqual(stats.packets_expected, 4)
        self.assertEqual(stats.packets_lost, 1)

    def test_fraction_lost(self) -> None:
        stats = StreamStatistics(clockrate=8000)
        stats.add(RtpPacket(sequence_number=0, timestamp=0))
        stats.add(RtpPacket(sequence_number=1, timestamp=160))

        # First call resets interval counters
        frac = stats.fraction_lost
        self.assertEqual(frac, 0)

        # Now skip a packet
        stats.add(RtpPacket(sequence_number=3, timestamp=480))
        frac = stats.fraction_lost
        # 2 expected in interval (seq 2 and 3), 1 received -> 1 lost
        # fraction = (1 << 8) // 2 = 128
        self.assertEqual(frac, 128)
