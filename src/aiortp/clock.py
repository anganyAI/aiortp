import asyncio
import datetime
import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

NTP_EPOCH = datetime.datetime(1900, 1, 1, tzinfo=datetime.UTC)


class MediaClock:
    """Calls *on_tick* every *interval* seconds on the running event loop.

    Deadlines are absolute (``loop.time()`` based), so a slow tick or
    scheduling delay does not accumulate drift across the stream.
    """

    def __init__(self, interval: float, on_tick: Callable[[], None]) -> None:
        self._interval = interval
        self._on_tick = on_tick
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._interval
        while True:
            await asyncio.sleep(max(0.0, deadline - loop.time()))
            deadline += self._interval
            try:
                self._on_tick()
            except Exception:
                # Same semantics as a raising transport callback: the
                # error is logged and the stream keeps running
                logger.exception("Media clock tick failed")


def current_datetime() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def current_ntp_time() -> int:
    return datetime_to_ntp(current_datetime())


def datetime_from_ntp(ntp: int) -> datetime.datetime:
    seconds = ntp >> 32
    microseconds = ((ntp & 0xFFFFFFFF) * 1000000) / (1 << 32)
    return NTP_EPOCH + datetime.timedelta(seconds=seconds, microseconds=microseconds)


def datetime_to_ntp(dt: datetime.datetime) -> int:
    delta = dt - NTP_EPOCH
    high = int(delta.total_seconds())
    low = round((delta.microseconds * (1 << 32)) // 1000000)
    return (high << 32) | low
