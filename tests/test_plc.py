import struct
from unittest import TestCase

from aiortp.plc import PcmConcealer


def _samples(pcm: bytes) -> tuple[int, ...]:
    return struct.unpack(f"<{len(pcm) // 2}h", pcm)


class PcmConcealerTest(TestCase):
    def test_no_history_is_silence(self) -> None:
        concealer = PcmConcealer(sample_rate=8000)
        self.assertEqual(concealer.frame_samples, 0)
        self.assertEqual(concealer.conceal(160), b"\x00" * 320)

    def test_repeats_last_frame_with_fade(self) -> None:
        concealer = PcmConcealer(sample_rate=8000)  # fade budget: 480 samples
        concealer.update(struct.pack("<160h", *([10000] * 160)))
        self.assertEqual(concealer.frame_samples, 160)

        samples = _samples(concealer.conceal(160))
        self.assertEqual(samples[0], 10000)  # full gain at fade start
        self.assertLess(samples[-1], 10000)  # attenuating
        self.assertGreater(samples[-1], 0)

        # Consecutive concealment keeps fading from where it left off
        more = _samples(concealer.conceal(160))
        self.assertLess(more[0], samples[-1])

    def test_fade_reaches_silence_after_budget(self) -> None:
        concealer = PcmConcealer(sample_rate=8000)
        concealer.update(struct.pack("<160h", *([10000] * 160)))

        concealer.conceal(480)  # exhaust the 60 ms fade budget
        self.assertEqual(concealer.conceal(160), b"\x00" * 320)

    def test_update_resets_fade_budget(self) -> None:
        concealer = PcmConcealer(sample_rate=8000)
        concealer.update(struct.pack("<160h", *([10000] * 160)))
        concealer.conceal(480)

        # A new real frame restores full concealment gain
        concealer.update(struct.pack("<160h", *([8000] * 160)))
        samples = _samples(concealer.conceal(160))
        self.assertEqual(samples[0], 8000)

    def test_output_length_matches_request(self) -> None:
        concealer = PcmConcealer(sample_rate=8000)
        concealer.update(struct.pack("<160h", *([100] * 160)))
        self.assertEqual(len(concealer.conceal(320)), 640)
