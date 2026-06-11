"""Adaptive playout buffer — clocked frame delivery over a jittery network.

AdaptivePlayout holds encoded frames keyed by RTP timestamp and serves
them on a fixed ptime tick: present frames are decoded, missing slots
are concealed, and the buffering depth follows measured network jitter
by inserting concealment (growth) or dropping a frame (reduction) at
frame granularity.

Frames are decoded at consumption time, not arrival time, so the
decoder sees decode and conceal calls in playout order (libopus PLC
state stays aligned) and late-dropped frames are never decoded.

The timestamp grid is ``codec.samples_per_frame`` — this library's
sender convention, including for G.722 where it deliberately diverges
from the RFC 3551 8 kHz RTP clock.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from .codecs import Codec
from .plc import PcmConcealer
from .utils import uint32_add, uint32_gt

logger = logging.getLogger(__name__)

# Frames that must be buffered before (re)starting delivery
PRIME_FRAMES = 2

# Consecutive concealment budget; beyond it the gap is treated as a
# sender pause (DTX, hold) or a long burst, not ordinary loss
DEFAULT_MAX_CONCEAL_MS = 120

# Ticks a depth deviation must persist before adjusting, and the
# minimum spacing between two adjustments
ADJUST_PERSIST_TICKS = 25
ADJUST_MIN_INTERVAL = 1.0


class AdaptivePlayout:
    """Pure playout logic — the asyncio side lives in clock.MediaClock."""

    def __init__(
        self,
        codec: Codec,
        max_delay_ms: int = 200,
        max_conceal_ms: int = DEFAULT_MAX_CONCEAL_MS,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._codec = codec
        self._concealer = PcmConcealer(sample_rate=codec.sample_rate)
        self._now = now
        self._increment = codec.samples_per_frame
        self._ptime_ms = 1000.0 * codec.samples_per_frame / codec.sample_rate
        self._max_frames = max(1, int(max_delay_ms / self._ptime_ms))
        self._max_conceal_ms = max_conceal_ms

        # Encoded frames awaiting playout, keyed by RTP timestamp
        self._frames: dict[int, bytes] = {}
        self._head: int | None = None

        # Interarrival jitter EWMA (RFC 3550 formula, in milliseconds)
        self._jitter_ms = 0.0
        self._last_arrival: float | None = None
        self._last_ts: int | None = None

        # Depth adaptation state
        self._deviation_sign = 0
        self._deviation_ticks = 0
        self._last_adjust = now()
        self._conceal_streak_ms = 0.0

        # Counters
        self.concealed_frames = 0
        self.late_dropped = 0
        self.expansions = 0
        self.accelerations = 0
        self.underrun_suspensions = 0

    @property
    def ptime(self) -> float:
        """Tick interval in seconds."""
        return self._ptime_ms / 1000.0

    @property
    def target_frames(self) -> int:
        return max(1, min(self._max_frames, round(2.0 * self._jitter_ms / self._ptime_ms) + 1))

    @property
    def depth_ms(self) -> float:
        return len(self._frames) * self._ptime_ms

    @property
    def target_ms(self) -> float:
        return self.target_frames * self._ptime_ms

    def stats(self) -> dict[str, float | int]:
        return {
            "concealed_frames": self.concealed_frames,
            "late_dropped": self.late_dropped,
            "expansions": self.expansions,
            "accelerations": self.accelerations,
            "underrun_suspensions": self.underrun_suspensions,
            "playout_delay_ms": round(self.depth_ms, 1),
            "playout_target_ms": round(self.target_ms, 1),
        }

    def reset(self) -> None:
        """Forget the stream position and buffered frames (new source)."""
        self._frames.clear()
        self._head = None
        self._jitter_ms = 0.0
        self._last_arrival = None
        self._last_ts = None
        self._conceal_streak_ms = 0.0
        self._deviation_ticks = 0

    def suppress(self) -> None:
        """Pause delivery on explicit sender suppression (e.g. DTMF digits).

        Unlike an underrun this is not loss: nothing is concealed and no
        suspension is counted.  Buffered frames are discarded so delivery
        re-primes on the post-suppression timeline.
        """
        self._frames.clear()
        self._head = None
        self._conceal_streak_ms = 0.0

    # ── Inbound ──────────────────────────────────────────────

    def put(self, timestamp: int, payload: bytes) -> None:
        """Buffer an encoded frame for playout."""
        if self._head is not None:
            if uint32_gt(self._head, timestamp):
                self.late_dropped += 1
                return
            ahead = uint32_add(timestamp, -self._head)
            if ahead > 4 * self._max_frames * self._increment:
                logger.info("Playout resync: timestamp jumped ahead by %d", ahead)
                self.reset()
        self._measure_jitter(timestamp)
        self._frames[timestamp] = payload
        if self._head is None and len(self._frames) >= PRIME_FRAMES:
            self._head = self._earliest()

    def _measure_jitter(self, timestamp: int) -> None:
        arrival = self._now()
        if self._last_arrival is not None and self._last_ts is not None:
            if uint32_gt(self._last_ts, timestamp):
                return  # reordered arrival — keep the newer reference
            ts_delta = uint32_add(timestamp, -self._last_ts)
            expected_ms = ts_delta / self._increment * self._ptime_ms
            deviation = abs((arrival - self._last_arrival) * 1000.0 - expected_ms)
            self._jitter_ms += (deviation - self._jitter_ms) / 16.0
        self._last_arrival = arrival
        self._last_ts = timestamp

    def _earliest(self) -> int:
        timestamps = iter(self._frames)
        best = next(timestamps)
        for ts in timestamps:
            if uint32_gt(best, ts):
                best = ts
        return best

    def _prune_stale(self) -> None:
        """Drop entries the head has walked past (off-grid timestamps).

        tick() only consumes exact grid matches, so a sender whose
        timestamps drift off the head grid would otherwise grow the
        buffer without bound.
        """
        assert self._head is not None
        stale = [ts for ts in self._frames if uint32_gt(self._head, ts)]
        for ts in stale:
            del self._frames[ts]
        self.late_dropped += len(stale)

    # ── Playout ──────────────────────────────────────────────

    def tick(self) -> tuple[bytes, int] | None:
        """Produce the next ptime of audio, or None while priming/suspended."""
        if self._head is None:
            return None
        if len(self._frames) > 2 * self._max_frames:
            self._prune_stale()
        adjust = self._update_adaptation()
        if adjust < 0 and self._concealer.frame_samples:
            # Expansion: deliver concealment without consuming, growing
            # the buffered depth by one frame
            self.expansions += 1
            return self._conceal_pcm(), self._head
        if adjust > 0 and self._head in self._frames:
            # Acceleration: drop the head frame, deliver the next one
            del self._frames[self._head]
            self._head = uint32_add(self._head, self._increment)
            self.accelerations += 1
        return self._consume()

    def _update_adaptation(self) -> int:
        """Return -1 to grow the buffer, +1 to shrink it, 0 to hold."""
        deviation = len(self._frames) - self.target_frames
        sign = (deviation > 0) - (deviation < 0)
        if sign != self._deviation_sign:
            self._deviation_sign = sign
            self._deviation_ticks = 1
            return 0
        self._deviation_ticks += 1
        if (
            sign == 0
            or self._deviation_ticks < ADJUST_PERSIST_TICKS
            or self._now() - self._last_adjust < ADJUST_MIN_INTERVAL
        ):
            return 0
        self._deviation_ticks = 0
        self._last_adjust = self._now()
        return sign

    def _consume(self) -> tuple[bytes, int] | None:
        assert self._head is not None
        out_ts = self._head
        payload = self._frames.pop(out_ts, None)
        if payload is None:
            return self._underrun(out_ts)
        self._conceal_streak_ms = 0.0
        self._head = uint32_add(out_ts, self._increment)
        try:
            pcm = self._codec.decode(payload)
        except Exception:
            logger.warning("Playout decode failed at ts=%d; concealing", out_ts)
            self.concealed_frames += 1
            return self._conceal_pcm(), out_ts
        self._concealer.update(pcm)
        return pcm, out_ts

    def _underrun(self, out_ts: int) -> tuple[bytes, int] | None:
        if self._conceal_streak_ms >= self._max_conceal_ms:
            self._prune_stale()
            if self._frames:
                # Long burst with later frames waiting: jump over the gap
                self._head = self._earliest()
                self._conceal_streak_ms = 0.0
                return self._consume()
            # Nothing buffered: sender pause — suspend until re-primed
            self.underrun_suspensions += 1
            self._head = None
            self._conceal_streak_ms = 0.0
            return None
        self._conceal_streak_ms += self._ptime_ms
        self.concealed_frames += 1
        self._head = uint32_add(out_ts, self._increment)
        return self._conceal_pcm(), out_ts

    def _conceal_pcm(self) -> bytes:
        samples = self._concealer.frame_samples or self._codec.samples_per_frame
        pcm = self._codec.conceal(samples)
        if pcm is None:
            pcm = self._concealer.conceal(samples)
        return pcm
