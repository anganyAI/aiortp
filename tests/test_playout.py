"""Tests for AdaptivePlayout — manual ticks, injected clock, deterministic."""

import struct

from aiortp.codecs import get_codec
from aiortp.playout import AdaptivePlayout

SPF = 160  # PCMU samples per frame (20 ms at 8 kHz)
PTIME = 0.020


class FakeTime:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _payload(value: int = 1000) -> bytes:
    return get_codec(0).encode(struct.pack("<160h", *([value] * SPF)))


def _playout(**kwargs: int) -> tuple[AdaptivePlayout, FakeTime]:
    clock = FakeTime()
    return AdaptivePlayout(get_codec(0), now=clock, **kwargs), clock


def test_priming_then_delivery() -> None:
    """No delivery until PRIME_FRAMES are buffered; then frames flow in order."""
    p, clock = _playout()
    assert p.tick() is None

    p.put(0, _payload())
    assert p.tick() is None  # one frame is not enough to prime

    clock.advance(PTIME)
    p.put(SPF, _payload())
    first = p.tick()
    second = p.tick()
    assert first is not None and first[1] == 0
    assert second is not None and second[1] == SPF
    assert len(first[0]) == SPF * 2  # decoded s16le PCM


def test_steady_state_no_concealment() -> None:
    """Regular arrivals produce a continuous grid without concealment."""
    p, clock = _playout()
    delivered: list[int] = []
    for i in range(10):
        p.put(i * SPF, _payload())
        clock.advance(PTIME)
        frame = p.tick()
        if frame is not None:
            delivered.append(frame[1])
    assert delivered == [i * SPF for i in range(len(delivered))]
    assert p.concealed_frames == 0
    assert p.late_dropped == 0


def test_underrun_concealment_keeps_grid() -> None:
    """A missing frame at its deadline is concealed and the grid advances."""
    p, clock = _playout()
    p.put(0, _payload())
    clock.advance(PTIME)
    p.put(SPF, _payload())
    assert p.tick() is not None
    assert p.tick() is not None

    concealed = p.tick()  # nothing buffered for ts=320
    assert concealed is not None
    assert concealed[1] == 2 * SPF
    assert len(concealed[0]) == SPF * 2
    assert p.concealed_frames == 1

    p.put(3 * SPF, _payload())  # stream resumes on the grid
    resumed = p.tick()
    assert resumed is not None and resumed[1] == 3 * SPF


def test_late_frame_dropped() -> None:
    """A frame older than the playout head is dropped, never decoded."""
    p, clock = _playout()
    p.put(0, _payload())
    p.put(SPF, _payload())
    assert p.tick() is not None  # head moves past ts=0

    p.put(0, _payload())
    assert p.late_dropped == 1


def test_suspension_after_conceal_budget_then_reprime() -> None:
    """120 ms of concealment with an empty buffer suspends delivery."""
    p, clock = _playout()
    p.put(0, _payload())
    p.put(SPF, _payload())
    assert p.tick() is not None
    assert p.tick() is not None

    conceals = 0
    while p.tick() is not None:
        conceals += 1
    assert conceals == 6  # 120 ms / 20 ms
    assert p.underrun_suspensions == 1
    assert p.tick() is None  # stays suspended

    # Two new frames re-prime delivery
    p.put(100 * SPF, _payload())
    p.put(101 * SPF, _payload())
    resumed = p.tick()
    assert resumed is not None and resumed[1] == 100 * SPF


def test_long_gap_jumps_to_buffered_frames() -> None:
    """When later frames wait behind a long gap, playout jumps instead of suspending."""
    p, clock = _playout()
    p.put(0, _payload())
    p.put(SPF, _payload())
    assert p.tick() is not None
    assert p.tick() is not None

    p.put(10 * SPF, _payload(2000))  # 8 frames missing in between
    delivered: list[int] = []
    for _ in range(7):
        frame = p.tick()
        assert frame is not None
        delivered.append(frame[1])
    # 6 concealed frames (budget), then the jump to the real frame
    assert delivered[:6] == [(2 + i) * SPF for i in range(6)]
    assert delivered[6] == 10 * SPF
    assert p.underrun_suspensions == 0


def test_suppress_pauses_without_concealment() -> None:
    """Explicit sender suppression (DTMF) stops delivery without counting loss."""
    p, clock = _playout()
    p.put(0, _payload())
    p.put(SPF, _payload())
    assert p.tick() is not None

    p.suppress()
    assert p.tick() is None
    assert p.concealed_frames == 0
    assert p.underrun_suspensions == 0

    p.put(50 * SPF, _payload())
    p.put(51 * SPF, _payload())
    resumed = p.tick()
    assert resumed is not None and resumed[1] == 50 * SPF


def test_expansion_under_jitter() -> None:
    """Sustained jitter raises the target depth; playout inserts concealment."""
    p, clock = _playout()
    delivered: list[int] = []
    # Alternating early/late arrivals: |deviation| = 19 ms on every put
    deltas = [0.001, 0.039]
    for i in range(60):
        p.put(i * SPF, _payload())
        clock.advance(deltas[i % 2])
        frame = p.tick()
        if frame is not None:
            delivered.append(frame[1])
    assert p.target_frames >= 2
    assert p.expansions >= 1
    # The expansion tick repeats the head timestamp before the real frame
    repeats = sum(1 for a, b in zip(delivered, delivered[1:], strict=False) if a == b)
    assert repeats >= 1


def test_acceleration_drains_excess_depth() -> None:
    """Depth persistently above target is shrunk by dropping one frame."""
    p, clock = _playout()
    for i in range(6):  # build excess depth before delivery starts
        p.put(i * SPF, _payload())
    delivered: list[int] = []
    for i in range(6, 66):
        p.put(i * SPF, _payload())
        clock.advance(PTIME)
        frame = p.tick()
        if frame is not None:
            delivered.append(frame[1])
    assert p.accelerations >= 1
    skips = sum(1 for a, b in zip(delivered, delivered[1:], strict=False) if b - a == 2 * SPF)
    assert skips == p.accelerations
    assert p.concealed_frames == 0


def test_timestamp_wraparound() -> None:
    """The head grid crosses the 2^32 boundary cleanly."""
    p, clock = _playout()
    ts0 = (1 << 32) - SPF
    p.put(ts0, _payload())
    p.put(0, _payload())
    first = p.tick()
    second = p.tick()
    assert first is not None and first[1] == ts0
    assert second is not None and second[1] == 0


def test_resync_on_far_timestamp_jump() -> None:
    """A timestamp far outside the window resets and re-primes the playout."""
    p, clock = _playout()
    p.put(0, _payload())
    p.put(SPF, _payload())
    assert p.tick() is not None

    far = 10_000_000
    p.put(far, _payload())
    assert p.tick() is None  # reset: waiting for re-prime
    p.put(far + SPF, _payload())
    resumed = p.tick()
    assert resumed is not None and resumed[1] == far


def test_stats_shape() -> None:
    p, clock = _playout(max_delay_ms=100)
    stats = p.stats()
    assert set(stats) == {
        "concealed_frames",
        "late_dropped",
        "expansions",
        "accelerations",
        "underrun_suspensions",
        "cn_frames",
        "playout_delay_ms",
        "playout_target_ms",
    }
    assert stats["playout_target_ms"] == 20.0  # one frame at zero jitter


def test_offgrid_entries_do_not_accumulate() -> None:
    """Timestamps off the head grid are pruned, not hoarded forever."""
    p, clock = _playout()
    for i in range(60):
        p.put(i * SPF, _payload())
        p.put(i * SPF + 80, _payload())  # off-grid: never matches the head
        clock.advance(PTIME)
        p.tick()
    assert len(p._frames) <= 2 * p._max_frames + 2
    assert p.late_dropped > 0  # pruned entries are accounted for


def test_gap_jump_ignores_stale_offgrid_entries() -> None:
    """The post-burst jump goes forward, never back to a stale timestamp."""
    p, clock = _playout()
    p.put(0, _payload())
    p.put(SPF, _payload())
    assert p.tick() is not None
    assert p.tick() is not None  # head = 320

    p.put(2 * SPF + 80, _payload(500))  # off-grid, soon behind the head
    p.put(20 * SPF, _payload(2000))  # the real resume point
    for _ in range(6):
        assert p.tick() is not None  # concealment budget
    resumed = p.tick()
    assert resumed is not None
    assert resumed[1] == 20 * SPF  # jumped forward, not to ts=400


def test_cn_state_generates_noise_on_the_grid() -> None:
    """From the CN timestamp on, missing slots yield noise, not concealment."""
    p, clock = _playout()
    p.put(0, _payload())
    p.put(SPF, _payload())
    assert p.tick() is not None
    assert p.tick() is not None  # head = 320

    p.set_cn(60, 2 * SPF)
    for i in range(10):
        frame = p.tick()
        assert frame is not None
        assert frame[1] == (2 + i) * SPF
        assert frame[0] != b"\x00" * len(frame[0])  # noise, not silence
    assert p.cn_frames == 10
    assert p.concealed_frames == 0
    assert p.underrun_suspensions == 0


def test_cn_applies_only_from_its_timestamp() -> None:
    """Frames buffered before the silence point still play; loss before it
    still conceals."""
    p, clock = _playout()
    p.put(0, _payload())
    p.put(SPF, _payload())
    p.put(3 * SPF, _payload())  # 2*SPF genuinely lost before the silence
    p.set_cn(70, 4 * SPF)  # silence starts after the buffered frames

    delivered = [p.tick() for _ in range(5)]
    assert all(f is not None for f in delivered)
    # 0 and 160 are real, 320 is concealed loss, 480 is real, then noise
    assert [f[1] for f in delivered] == [0, SPF, 2 * SPF, 3 * SPF, 4 * SPF]  # type: ignore[index]
    assert p.concealed_frames == 1
    assert p.cn_frames == 1


def test_cn_exits_on_resumed_media() -> None:
    p, clock = _playout()
    p.put(0, _payload())
    p.put(SPF, _payload())
    assert p.tick() is not None
    assert p.tick() is not None
    p.set_cn(60, 2 * SPF)
    for _ in range(3):
        assert p.tick() is not None  # noise: head reaches 5*SPF

    p.put(5 * SPF, _payload())  # talkspurt resumes
    resumed = p.tick()
    assert resumed is not None and resumed[1] == 5 * SPF
    assert p.cn_frames == 3
    # CN state is gone: the next missing slot is loss again
    assert p.tick() is not None
    assert p.concealed_frames == 1


def test_cn_from_suspension_restarts_timeline() -> None:
    p, clock = _playout()
    p.put(0, _payload())
    p.put(SPF, _payload())
    assert p.tick() is not None
    assert p.tick() is not None
    while p.tick() is not None:  # conceal budget then suspension
        pass
    assert p.underrun_suspensions == 1

    p.set_cn(70, 1000 * SPF)
    frame = p.tick()
    assert frame is not None
    assert frame[1] == 1001 * SPF  # grid restarts after the CN packet
    assert p.cn_frames == 1


def test_cn_timeout_suspends() -> None:
    """30 s of noise without a single packet means a dead sender."""
    p, clock = _playout()
    p.set_cn(70, 0)
    count = 0
    while p.tick() is not None:
        count += 1
    assert count == 1500  # 30 s at 20 ms
    assert p.underrun_suspensions == 1
    assert p.tick() is None


def test_suppress_and_reset_clear_cn() -> None:
    p, clock = _playout()
    p.set_cn(70, 0)
    assert p.tick() is not None
    p.suppress()
    assert p.tick() is None

    p.set_cn(70, 0)
    assert p.tick() is not None
    p.reset()
    assert p.tick() is None


def test_cn_refresh_does_not_reintroduce_concealment() -> None:
    """Refresh packets carry later timestamps; the silence origin must
    stay pinned or the head falls back into concealment every 3 s."""
    p, clock = _playout()
    p.put(0, _payload())
    p.put(SPF, _payload())
    assert p.tick() is not None
    assert p.tick() is not None  # head = 320

    p.set_cn(60, 2 * SPF)
    for _ in range(3):
        assert p.tick() is not None  # noise; head = 5*SPF

    p.set_cn(60, 10 * SPF)  # refresh with a timestamp ahead of the head
    frame = p.tick()
    assert frame is not None
    assert p.cn_frames == 4  # still noise
    assert p.concealed_frames == 0  # no phantom concealment
