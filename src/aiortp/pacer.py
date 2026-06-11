"""Paced RTP sending — a clocked queue emitting one frame per ptime.

Callers enqueue encoded frames at any rate; transmission happens on the
media clock.  Empty ticks advance the RTP timestamp without emitting a
packet, so silence appears on the wire as a timestamp jump (RFC 3550)
rather than stale or bursty audio.  The queue is unbounded: streaming a
whole file ahead of real time is a supported pattern — ``queue_depth``
exposes the backlog.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable

from .cn import DEFAULT_CN_LEVEL, build_cn_payload
from .sender import RtpSender

# One comfort-noise refresh every 3 s of silence at 20 ms ticks
_CN_REFRESH_TICKS = 150


class PacedSender:
    """Pure pacing logic — driven by clock.MediaClock at ptime intervals.

    With *cn_payload_type* set, entering a silence period (empty queue
    after at least one audio frame) emits an RFC 3389 comfort-noise
    packet, refreshed every 3 s while the silence lasts.
    """

    def __init__(
        self,
        sender: RtpSender,
        get_addr: Callable[[], tuple[str, int] | None],
        cn_payload_type: int | None = None,
        get_cn_level: Callable[[], int] | None = None,
    ) -> None:
        self._sender = sender
        self._get_addr = get_addr
        self._cn_payload_type = cn_payload_type
        self._get_cn_level = get_cn_level
        self._queue: deque[bytes] = deque()
        self._empty = asyncio.Event()
        self._empty.set()
        self._silence_ticks = 0
        self.paced_sent = 0
        self.empty_ticks = 0
        self.cn_sent = 0

    @property
    def queue_depth(self) -> int:
        return len(self._queue)

    def enqueue(self, payload: bytes) -> None:
        self._queue.append(payload)
        self._empty.clear()

    async def drain(self) -> None:
        """Wait until every queued frame has been transmitted."""
        await self._empty.wait()

    def discard(self) -> None:
        """Drop queued frames and release drain() waiters.

        Without this, stopping the media clock with a non-empty queue
        would leave drain() callers waiting forever.
        """
        self._queue.clear()
        self._empty.set()

    def tick(self) -> None:
        if self._queue:
            payload = self._queue.popleft()
            # Marker flags the start of a talkspurt after silence (RFC 3551)
            marker = 1 if self._silence_ticks and self.paced_sent else 0
            self._sender.send_frame_auto(payload, marker=marker, addr=self._get_addr())
            self.paced_sent += 1
            self._silence_ticks = 0
            if not self._queue:
                self._empty.set()
            return
        if (
            self._cn_payload_type is not None
            and self.paced_sent  # silence follows talk, not session start
            and self._silence_ticks % _CN_REFRESH_TICKS == 0
        ):
            self._send_cn()
        self._silence_ticks += 1
        self._sender.advance_timestamp()
        self.empty_ticks += 1

    def _send_cn(self) -> None:
        assert self._cn_payload_type is not None
        level = self._get_cn_level() if self._get_cn_level is not None else DEFAULT_CN_LEVEL
        self._sender.send_raw(
            self._cn_payload_type,
            build_cn_payload(level),
            self._sender.current_timestamp,
            addr=self._get_addr(),
        )
        self.cn_sent += 1

    def stats(self) -> dict[str, int]:
        return {
            "queue_depth": self.queue_depth,
            "paced_sent": self.paced_sent,
            "empty_ticks": self.empty_ticks,
            "cn_sent": self.cn_sent,
        }
