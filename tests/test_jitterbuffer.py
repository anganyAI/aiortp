from typing import Optional
from unittest import TestCase

from aiortp.jitterbuffer import JitterBuffer
from aiortp.packet import RtpPacket


class JitterBufferTest(TestCase):
    def assertPackets(
        self, jbuffer: JitterBuffer, expected: list[Optional[int]]
    ) -> None:
        found = [x.sequence_number if x else None for x in jbuffer._packets]
        self.assertEqual(found, expected)

    def test_create(self) -> None:
        jbuffer = JitterBuffer(capacity=2)
        self.assertEqual(jbuffer._packets, [None, None])
        self.assertEqual(jbuffer._origin, None)

        jbuffer = JitterBuffer(capacity=4)
        self.assertEqual(jbuffer._packets, [None, None, None, None])
        self.assertEqual(jbuffer._origin, None)

    def test_add_ordered(self) -> None:
        jbuffer = JitterBuffer(capacity=4)

        pli_flag, frame = jbuffer.add(RtpPacket(sequence_number=0, timestamp=1234))
        self.assertIsNone(frame)
        self.assertPackets(jbuffer, [0, None, None, None])
        self.assertEqual(jbuffer._origin, 0)
        self.assertFalse(pli_flag)

        pli_flag, frame = jbuffer.add(RtpPacket(sequence_number=1, timestamp=1234))
        self.assertIsNone(frame)
        self.assertPackets(jbuffer, [0, 1, None, None])
        self.assertEqual(jbuffer._origin, 0)
        self.assertFalse(pli_flag)

        pli_flag, frame = jbuffer.add(RtpPacket(sequence_number=2, timestamp=1234))
        self.assertIsNone(frame)
        self.assertPackets(jbuffer, [0, 1, 2, None])
        self.assertEqual(jbuffer._origin, 0)
        self.assertFalse(pli_flag)

        pli_flag, frame = jbuffer.add(RtpPacket(sequence_number=3, timestamp=1234))
        self.assertIsNone(frame)
        self.assertPackets(jbuffer, [0, 1, 2, 3])
        self.assertEqual(jbuffer._origin, 0)
        self.assertFalse(pli_flag)

    def test_add_unordered(self) -> None:
        jbuffer = JitterBuffer(capacity=4)

        pli_flag, frame = jbuffer.add(RtpPacket(sequence_number=1, timestamp=1234))
        self.assertIsNone(frame)
        self.assertPackets(jbuffer, [None, 1, None, None])
        self.assertEqual(jbuffer._origin, 1)
        self.assertFalse(pli_flag)

        pli_flag, frame = jbuffer.add(RtpPacket(sequence_number=3, timestamp=1234))
        self.assertIsNone(frame)
        self.assertPackets(jbuffer, [None, 1, None, 3])
        self.assertEqual(jbuffer._origin, 1)
        self.assertFalse(pli_flag)

        pli_flag, frame = jbuffer.add(RtpPacket(sequence_number=2, timestamp=1234))
        self.assertIsNone(frame)
        self.assertPackets(jbuffer, [None, 1, 2, 3])
        self.assertEqual(jbuffer._origin, 1)
        self.assertFalse(pli_flag)

    def test_add_seq_too_low_drop(self) -> None:
        jbuffer = JitterBuffer(capacity=4)

        pli_flag, frame = jbuffer.add(RtpPacket(sequence_number=2, timestamp=1234))
        self.assertIsNone(frame)
        self.assertPackets(jbuffer, [None, None, 2, None])
        self.assertEqual(jbuffer._origin, 2)
        self.assertFalse(pli_flag)

        pli_flag, frame = jbuffer.add(RtpPacket(sequence_number=1, timestamp=1234))
        self.assertIsNone(frame)
        self.assertPackets(jbuffer, [None, None, 2, None])
        self.assertEqual(jbuffer._origin, 2)
        self.assertFalse(pli_flag)

    def test_add_seq_too_low_reset(self) -> None:
        jbuffer = JitterBuffer(capacity=4)

        pli_flag, frame = jbuffer.add(RtpPacket(sequence_number=2000, timestamp=1234))
        self.assertIsNone(frame)
        self.assertPackets(jbuffer, [2000, None, None, None])
        self.assertEqual(jbuffer._origin, 2000)
        self.assertFalse(pli_flag)

        pli_flag, frame = jbuffer.add(RtpPacket(sequence_number=1, timestamp=1234))
        self.assertIsNone(frame)
        self.assertPackets(jbuffer, [None, 1, None, None])
        self.assertEqual(jbuffer._origin, 1)
        self.assertFalse(pli_flag)

    def test_add_seq_too_high_discard_one(self) -> None:
        jbuffer = JitterBuffer(capacity=4)

        jbuffer.add(RtpPacket(sequence_number=0, timestamp=1234))
        self.assertEqual(jbuffer._origin, 0)

        jbuffer.add(RtpPacket(sequence_number=1, timestamp=1234))
        self.assertEqual(jbuffer._origin, 0)

        jbuffer.add(RtpPacket(sequence_number=2, timestamp=1234))
        self.assertEqual(jbuffer._origin, 0)

        jbuffer.add(RtpPacket(sequence_number=3, timestamp=1234))
        self.assertEqual(jbuffer._origin, 0)

        jbuffer.add(RtpPacket(sequence_number=4, timestamp=1234))
        self.assertEqual(jbuffer._origin, 4)

        self.assertPackets(jbuffer, [4, None, None, None])

    def test_add_seq_too_high_discard_one_v2(self) -> None:
        jbuffer = JitterBuffer(capacity=4)

        jbuffer.add(RtpPacket(sequence_number=0, timestamp=1234))
        self.assertEqual(jbuffer._origin, 0)

        jbuffer.add(RtpPacket(sequence_number=2, timestamp=1234))
        self.assertEqual(jbuffer._origin, 0)

        jbuffer.add(RtpPacket(sequence_number=3, timestamp=1235))
        self.assertEqual(jbuffer._origin, 0)

        jbuffer.add(RtpPacket(sequence_number=4, timestamp=1235))
        self.assertEqual(jbuffer._origin, 3)

        self.assertPackets(jbuffer, [4, None, None, 3])

    def test_add_seq_too_high_discard_four(self) -> None:
        jbuffer = JitterBuffer(capacity=4)

        jbuffer.add(RtpPacket(sequence_number=0, timestamp=1234))
        self.assertEqual(jbuffer._origin, 0)

        jbuffer.add(RtpPacket(sequence_number=1, timestamp=1234))
        self.assertEqual(jbuffer._origin, 0)

        jbuffer.add(RtpPacket(sequence_number=3, timestamp=1234))
        self.assertEqual(jbuffer._origin, 0)

        jbuffer.add(RtpPacket(sequence_number=7, timestamp=1235))
        self.assertEqual(jbuffer._origin, 7)

        self.assertPackets(jbuffer, [None, None, None, 7])

    def test_add_seq_too_high_discard_more(self) -> None:
        jbuffer = JitterBuffer(capacity=4)

        jbuffer.add(RtpPacket(sequence_number=0, timestamp=1234))
        self.assertEqual(jbuffer._origin, 0)

        jbuffer.add(RtpPacket(sequence_number=1, timestamp=1234))
        self.assertEqual(jbuffer._origin, 0)

        jbuffer.add(RtpPacket(sequence_number=2, timestamp=1234))
        self.assertEqual(jbuffer._origin, 0)

        jbuffer.add(RtpPacket(sequence_number=3, timestamp=1234))
        self.assertEqual(jbuffer._origin, 0)

        jbuffer.add(RtpPacket(sequence_number=8, timestamp=1234))
        self.assertEqual(jbuffer._origin, 8)

        self.assertPackets(jbuffer, [8, None, None, None])

    def test_add_seq_too_high_reset(self) -> None:
        jbuffer = JitterBuffer(capacity=4)

        jbuffer.add(RtpPacket(sequence_number=0, timestamp=1234))
        self.assertEqual(jbuffer._origin, 0)
        self.assertPackets(jbuffer, [0, None, None, None])

        jbuffer.add(RtpPacket(sequence_number=3000, timestamp=1234))
        self.assertEqual(jbuffer._origin, 3000)
        self.assertPackets(jbuffer, [3000, None, None, None])

    def test_remove(self) -> None:
        jbuffer = JitterBuffer(capacity=4)

        jbuffer.add(RtpPacket(sequence_number=0, timestamp=1234))
        jbuffer.add(RtpPacket(sequence_number=1, timestamp=1234))
        jbuffer.add(RtpPacket(sequence_number=2, timestamp=1234))
        jbuffer.add(RtpPacket(sequence_number=3, timestamp=1234))
        self.assertEqual(jbuffer._origin, 0)
        self.assertPackets(jbuffer, [0, 1, 2, 3])

        # remove 1 packet
        jbuffer.remove(1)
        self.assertEqual(jbuffer._origin, 1)
        self.assertPackets(jbuffer, [None, 1, 2, 3])

        # remove 2 packets
        jbuffer.remove(2)
        self.assertEqual(jbuffer._origin, 3)
        self.assertPackets(jbuffer, [None, None, None, 3])

    def test_smart_remove(self) -> None:
        jbuffer = JitterBuffer(capacity=4)

        jbuffer.add(RtpPacket(sequence_number=0, timestamp=1234))
        jbuffer.add(RtpPacket(sequence_number=1, timestamp=1234))
        jbuffer.add(RtpPacket(sequence_number=3, timestamp=1235))
        self.assertEqual(jbuffer._origin, 0)
        self.assertPackets(jbuffer, [0, 1, None, 3])

        # remove 1 packet
        jbuffer.smart_remove(1)
        self.assertEqual(jbuffer._origin, 3)
        self.assertPackets(jbuffer, [None, None, None, 3])

    def test_remove_audio_frame(self) -> None:
        """Audio jitter buffer."""
        jbuffer = JitterBuffer(capacity=16, prefetch=4)

        pli_flag, frame = jbuffer.add(
            RtpPacket(sequence_number=0, timestamp=1234, payload=b"0000")
        )
        self.assertIsNone(frame)

        pli_flag, frame = jbuffer.add(
            RtpPacket(sequence_number=1, timestamp=1235, payload=b"0001")
        )
        self.assertIsNone(frame)

        pli_flag, frame = jbuffer.add(
            RtpPacket(sequence_number=2, timestamp=1236, payload=b"0002")
        )
        self.assertIsNone(frame)

        pli_flag, frame = jbuffer.add(
            RtpPacket(sequence_number=3, timestamp=1237, payload=b"0003")
        )
        self.assertIsNone(frame)

        pli_flag, frame = jbuffer.add(
            RtpPacket(sequence_number=4, timestamp=1238, payload=b"0003")
        )
        self.assertIsNotNone(frame)
        self.assertEqual(frame.data, b"0000")
        self.assertEqual(frame.timestamp, 1234)

        pli_flag, frame = jbuffer.add(
            RtpPacket(sequence_number=5, timestamp=1239, payload=b"0004")
        )
        self.assertIsNotNone(frame)
        self.assertEqual(frame.data, b"0001")
        self.assertEqual(frame.timestamp, 1235)

    def test_remove_video_frame(self) -> None:
        """Video jitter buffer."""
        jbuffer = JitterBuffer(capacity=128, is_video=True)

        pli_flag, frame = jbuffer.add(
            RtpPacket(sequence_number=0, timestamp=1234, payload=b"0000")
        )
        self.assertIsNone(frame)

        pli_flag, frame = jbuffer.add(
            RtpPacket(sequence_number=1, timestamp=1234, payload=b"0001")
        )
        self.assertIsNone(frame)

        pli_flag, frame = jbuffer.add(
            RtpPacket(sequence_number=2, timestamp=1234, payload=b"0002")
        )
        self.assertIsNone(frame)

        pli_flag, frame = jbuffer.add(
            RtpPacket(sequence_number=3, timestamp=1235, payload=b"0003")
        )
        self.assertIsNotNone(frame)
        self.assertEqual(frame.data, b"000000010002")
        self.assertEqual(frame.timestamp, 1234)

    def test_pli_flag(self) -> None:
        """Video jitter buffer."""
        jbuffer = JitterBuffer(capacity=128, is_video=True)

        pli_flag, frame = jbuffer.add(RtpPacket(sequence_number=2000, timestamp=1234))
        self.assertIsNone(frame)
        self.assertEqual(jbuffer._origin, 2000)
        self.assertFalse(pli_flag)

        # test_add_seq_too_low_reset for video (capacity >= 128)
        pli_flag, frame = jbuffer.add(RtpPacket(sequence_number=1, timestamp=1234))
        self.assertIsNone(frame)
        self.assertEqual(jbuffer._origin, 1)
        self.assertTrue(pli_flag)

        pli_flag, frame = jbuffer.add(RtpPacket(sequence_number=128, timestamp=1235))
        self.assertIsNone(frame)
        self.assertEqual(jbuffer._origin, 1)
        self.assertFalse(pli_flag)

        # test_add_seq_too_high_discard_one for video (capacity >= 128)
        pli_flag, frame = jbuffer.add(RtpPacket(sequence_number=129, timestamp=1235))
        self.assertIsNone(frame)
        self.assertEqual(jbuffer._origin, 128)
        self.assertTrue(pli_flag)

        # test_add_seq_too_high_reset for video (capacity >= 128)
        pli_flag, frame = jbuffer.add(RtpPacket(sequence_number=2000, timestamp=2345))
        self.assertIsNone(frame)
        self.assertEqual(jbuffer._origin, 2000)
        self.assertTrue(pli_flag)


class AudioGapHandlingTest(TestCase):
    """Tests for skip_audio_gaps mode — skips lost packets instead of stalling."""

    def test_gap_skipped_with_prefetch_zero(self) -> None:
        """Single lost packet is skipped; first frame delivered when gap detected."""
        jbuffer = JitterBuffer(capacity=16, prefetch=0, skip_audio_gaps=True)

        # seq0 arrives — no frame yet (need to see next timestamp)
        _, frame = jbuffer.add(RtpPacket(sequence_number=0, timestamp=100, payload=b"A"))
        self.assertIsNone(frame)

        # seq1 lost — seq2 arrives.  Gap detected with different timestamp
        # → seq0 delivered immediately (prefetch=0)
        _, frame = jbuffer.add(RtpPacket(sequence_number=2, timestamp=300, payload=b"C"))
        self.assertIsNotNone(frame)
        self.assertEqual(frame.data, b"A")
        self.assertEqual(frame.timestamp, 100)

        # seq3 arrives — seq2 delivered (origin skipped past the gap)
        _, frame = jbuffer.add(RtpPacket(sequence_number=3, timestamp=400, payload=b"D"))
        self.assertIsNotNone(frame)
        self.assertEqual(frame.data, b"C")
        self.assertEqual(frame.timestamp, 300)

    def test_gap_delivers_subsequent_frames(self) -> None:
        """After the gap, subsequent frames are delivered continuously."""
        jbuffer = JitterBuffer(capacity=16, prefetch=0, skip_audio_gaps=True)

        _, frame = jbuffer.add(RtpPacket(sequence_number=0, timestamp=100, payload=b"A"))
        self.assertIsNone(frame)

        # Gap at seq1 detected → seq0 delivered
        _, frame = jbuffer.add(RtpPacket(sequence_number=2, timestamp=300, payload=b"C"))
        self.assertIsNotNone(frame)
        self.assertEqual(frame.data, b"A")

        # seq3 delivers seq2
        _, frame = jbuffer.add(RtpPacket(sequence_number=3, timestamp=400, payload=b"D"))
        self.assertIsNotNone(frame)
        self.assertEqual(frame.data, b"C")

        # seq4 delivers seq3
        _, frame = jbuffer.add(RtpPacket(sequence_number=4, timestamp=500, payload=b"E"))
        self.assertIsNotNone(frame)
        self.assertEqual(frame.data, b"D")

    def test_gap_with_prefetch(self) -> None:
        """With prefetch=2, gap counts as boundary toward prefetch count."""
        jbuffer = JitterBuffer(capacity=16, prefetch=2, skip_audio_gaps=True)

        # seq0, seq1 arrive; seq2 lost; seq3 arrives
        jbuffer.add(RtpPacket(sequence_number=0, timestamp=100, payload=b"A"))
        jbuffer.add(RtpPacket(sequence_number=1, timestamp=200, payload=b"B"))
        # seq2 lost

        # seq3 triggers gap detection: seq0→seq1 (boundary=1), gap (boundary=2)
        # With prefetch=2, 2 boundaries >= 2 → seq0 delivered
        _, frame = jbuffer.add(RtpPacket(sequence_number=3, timestamp=400, payload=b"D"))
        self.assertIsNotNone(frame)
        self.assertEqual(frame.data, b"A")
        self.assertEqual(frame.timestamp, 100)

    def test_multiple_consecutive_gaps_limited(self) -> None:
        """More than MAX_AUDIO_GAP consecutive gaps → stop scanning."""
        jbuffer = JitterBuffer(capacity=16, prefetch=0, skip_audio_gaps=True)

        # seq0 arrives, then 4 consecutive losses (exceeds MAX_AUDIO_GAP=3)
        jbuffer.add(RtpPacket(sequence_number=0, timestamp=100, payload=b"A"))
        # seq1, seq2, seq3, seq4 all lost — gap of 4

        # seq5 arrives but gap is too large to scan past
        _, frame = jbuffer.add(RtpPacket(sequence_number=5, timestamp=600, payload=b"F"))
        self.assertIsNone(frame)

    def test_no_gap_handling_without_flag(self) -> None:
        """Without skip_audio_gaps, a gap blocks delivery (original behavior)."""
        jbuffer = JitterBuffer(capacity=16, prefetch=0)

        jbuffer.add(RtpPacket(sequence_number=0, timestamp=100, payload=b"A"))
        # seq1 lost
        jbuffer.add(RtpPacket(sequence_number=2, timestamp=300, payload=b"C"))
        _, frame = jbuffer.add(RtpPacket(sequence_number=3, timestamp=400, payload=b"D"))
        self.assertIsNone(frame)  # blocked by gap — old behavior

    def test_same_timestamp_gap_not_treated_as_audio_gap(self) -> None:
        """Same-timestamp packets with a gap don't trigger audio frame delivery."""
        jbuffer = JitterBuffer(capacity=16, prefetch=0, skip_audio_gaps=True)

        jbuffer.add(RtpPacket(sequence_number=0, timestamp=100, payload=b"A"))
        # seq1 lost, but seq2 has SAME timestamp — not a new audio frame
        _, frame = jbuffer.add(RtpPacket(sequence_number=2, timestamp=100, payload=b"C"))
        self.assertIsNone(frame)  # same ts → not a frame boundary
