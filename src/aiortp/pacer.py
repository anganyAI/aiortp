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

from .sender import RtpSender


class PacedSender:
    """Pure pacing logic — driven by clock.MediaClock at ptime intervals."""

    def __init__(
        self,
        sender: RtpSender,
        get_addr: Callable[[], tuple[str, int] | None],
    ) -> None:
        self._sender = sender
        self._get_addr = get_addr
        self._queue: deque[bytes] = deque()
        self._empty = asyncio.Event()
        self._empty.set()
        self.paced_sent = 0
        self.empty_ticks = 0

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
        if not self._queue:
            self._sender.advance_timestamp()
            self.empty_ticks += 1
            return
        payload = self._queue.popleft()
        self._sender.send_frame_auto(payload, addr=self._get_addr())
        self.paced_sent += 1
        if not self._queue:
            self._empty.set()

    def stats(self) -> dict[str, int]:
        return {
            "queue_depth": self.queue_depth,
            "paced_sent": self.paced_sent,
            "empty_ticks": self.empty_ticks,
        }
