from .packet import RtpPacket
from .utils import uint16_add

MAX_MISORDER = 100

# Identity marker for sequence numbers consumed by non-media packets
# (RFC 4733 telephone-events): the slot takes part in gap analysis but
# is never counted as lost nor assembled into a frame.
_NON_MEDIA = RtpPacket()


class JitterFrame:
    def __init__(self, data: bytes, timestamp: int, lost: int = 0) -> None:
        self.data = data
        self.timestamp = timestamp
        self.lost = lost  # confirmed-lost packets immediately before this frame


class JitterBuffer:
    def __init__(
        self,
        capacity: int,
        prefetch: int = 0,
        is_video: bool = False,
        skip_audio_gaps: bool = False,
    ) -> None:
        assert capacity & (capacity - 1) == 0, "capacity must be a power of 2"
        self._capacity = capacity
        self._origin: int | None = None
        self._packets: list[RtpPacket | None] = [None for i in range(capacity)]
        self._prefetch = prefetch
        self._is_video = is_video
        self._skip_audio_gaps = skip_audio_gaps and not is_video
        self._video_gap_skipped = False  # set when incomplete frame is dropped

    @property
    def capacity(self) -> int:
        return self._capacity

    def reset(self) -> None:
        """Discard buffered packets and forget the sequence origin (new source)."""
        self._packets = [None] * self._capacity
        self._origin = None

    def add(self, packet: RtpPacket) -> tuple[bool, JitterFrame | None]:
        pli_flag, stored = self._place(packet, packet.sequence_number)
        if not stored:
            return pli_flag, None

        self._video_gap_skipped = False
        frame = self._remove_frame(packet.sequence_number)
        if self._video_gap_skipped:
            pli_flag = True
        return pli_flag, frame

    def mark_non_media(self, sequence_number: int) -> JitterFrame | None:
        """Occupy *sequence_number* without media (e.g. RFC 4733 packets).

        Telephone-event packets share the audio sequence space; without a
        marker their slots read as packet loss.  The marker also acts as a
        frame boundary, so it may complete a pending frame — returned here.
        """
        _, stored = self._place(_NON_MEDIA, sequence_number)
        if not stored:
            return None
        return self._remove_frame(sequence_number)

    def _place(self, packet: RtpPacket, sequence_number: int) -> tuple[bool, bool]:
        """Store *packet* at its slot, managing origin, misorder and capacity.

        Returns (pli_flag, stored).
        """
        pli_flag = False
        if self._origin is None:
            self._origin = sequence_number
            delta = 0
            misorder = 0
        else:
            delta = uint16_add(sequence_number, -self._origin)
            misorder = uint16_add(self._origin, -sequence_number)

        if misorder < delta:
            if misorder >= MAX_MISORDER:
                self.remove(self.capacity)
                self._origin = sequence_number
                delta = misorder = 0
                if self._is_video:
                    pli_flag = True
            else:
                return pli_flag, False

        if delta >= self.capacity:
            # remove just enough frames to fit the received packets
            excess = delta - self.capacity + 1
            if self.smart_remove(excess):
                self._origin = sequence_number
            if self._is_video:
                pli_flag = True

        self._packets[sequence_number % self._capacity] = packet
        return pli_flag, True

    def _remove_frame(self, sequence_number: int) -> JitterFrame | None:
        if self._is_video:
            return self._remove_video_frame()
        return self._remove_audio_frame()

    def _remove_video_frame(self) -> JitterFrame | None:
        """Remove a complete video frame using the RTP marker bit.

        RFC 3550 defines marker=1 as the last packet of a video frame.
        This avoids waiting for the next timestamp, so frames are
        delivered as soon as the last packet arrives.

        If a gap is detected and a later packet exists (confirming the
        gap is a lost packet, not just not-yet-arrived), the incomplete
        frame is skipped and the scan restarts from the next received
        packet.
        """
        packets: list[RtpPacket] = []

        for count in range(self.capacity):
            pos = (self._origin + count) % self._capacity  # type: ignore[operator]
            packet = self._packets[pos]

            if packet is None:
                # Check if a packet from a LATER frame exists after the
                # gap.  If so, the current frame is lost — skip it.
                # If the later packet has the same timestamp, the missing
                # packet might still arrive (reordering) — wait.
                current_ts = packets[0].timestamp if packets else None
                if self._has_newer_video_frame_after(count, current_ts):
                    self._video_gap_skipped = True
                    self.remove(count + 1)
                    return self._remove_video_frame()
                break  # no evidence of loss yet — wait

            packets.append(packet)

            if packet.marker:
                self.remove(count + 1)
                return JitterFrame(
                    data=b"".join([p.payload for p in packets]),
                    timestamp=packets[0].timestamp,
                )

        return None

    def _has_newer_video_frame_after(
        self,
        gap_offset: int,
        current_ts: int | None,
    ) -> bool:
        """Check if a packet from a *different* frame exists after the gap.

        Returns True only if a later packet has a different RTP
        timestamp, confirming the current frame's gap is a real loss
        (not just reordering within the same frame).
        """
        for g in range(1, min(self._capacity - gap_offset, 64)):
            total = gap_offset + g
            if total >= self._capacity:
                return False
            pos = (self._origin + total) % self._capacity  # type: ignore[operator]
            pkt = self._packets[pos]
            if pkt is not None:
                if current_ts is None or pkt.timestamp != current_ts:
                    return True
        return False

    def _remove_audio_frame(self) -> JitterFrame | None:
        """Remove a complete audio frame using timestamp boundaries."""
        frame = None
        frames = 0
        packets: list[RtpPacket] = []
        remove = 0
        timestamp = None
        lost = 0

        for count in range(self.capacity):
            pos = (self._origin + count) % self._capacity  # type: ignore[operator]
            packet = self._packets[pos]
            if packet is None or packet is _NON_MEDIA:
                is_gap = packet is None
                confirmed = self._skip_audio_gaps and self._has_later_packet(count, timestamp)
                if is_gap and not confirmed:
                    break
                # Slot consumed without media: a confirmed-lost packet or a
                # non-media marker (RFC 4733).  Both complete the current
                # in-progress frame; only true gaps count as lost.
                if packets:
                    if frame is None:
                        frame = JitterFrame(
                            data=b"".join([x.payload for x in packets]),
                            timestamp=timestamp,
                            lost=lost,
                        )
                        remove = count
                    frames += 1
                    if frames >= self._prefetch:
                        self.remove(remove)
                        return frame
                    packets = []
                    timestamp = None
                elif frame is None and is_gap:
                    # Confirmed-lost slot ahead of the next frame — count
                    # it so the delivered frame reports the missing audio.
                    lost += 1
                continue

            if timestamp is None:
                timestamp = packet.timestamp
            elif packet.timestamp != timestamp:
                # we now have a complete frame, only store the first one
                if frame is None:
                    frame = JitterFrame(
                        data=b"".join([x.payload for x in packets]),
                        timestamp=timestamp,
                        lost=lost,
                    )
                    remove = count

                # check we have prefetched enough
                frames += 1
                if frames >= self._prefetch:
                    self.remove(remove)
                    return frame

                # start a new frame
                packets = []
                timestamp = packet.timestamp

            packets.append(packet)

        return None

    def _has_later_packet(self, gap_offset: int, current_timestamp: int | None = None) -> bool:
        """Check if a received packet with a *different* timestamp exists after the gap.

        Scans the remaining capacity, so loss bursts of any length the
        buffer can hold are confirmed.  Same-timestamp packets (e.g. video
        fragments) after the gap do not confirm a frame boundary when
        ``skip_audio_gaps`` is enabled.
        """
        for g in range(1, self._capacity - gap_offset):
            pos = (self._origin + gap_offset + g) % self._capacity  # type: ignore[operator]
            pkt = self._packets[pos]
            if pkt is None:
                continue
            if pkt is _NON_MEDIA:
                # A received non-media packet proves later delivery
                return True
            # Only skip if the packet after the gap starts a new frame
            if current_timestamp is not None and pkt.timestamp == current_timestamp:
                return False
            return True
        return False

    def remove(self, count: int) -> None:
        assert count <= self._capacity
        for _i in range(count):
            pos = self._origin % self._capacity  # type: ignore[operator]
            self._packets[pos] = None
            self._origin = uint16_add(self._origin, 1)  # type: ignore[arg-type]

    def smart_remove(self, count: int) -> bool:
        """
        Makes sure that all packages belonging to the same frame are removed
        to prevent sending corrupted frames to the decoder.
        """
        timestamp = None
        for i in range(self._capacity):
            pos = self._origin % self._capacity  # type: ignore[operator]
            packet = self._packets[pos]
            if packet is not None and packet is not _NON_MEDIA:
                if i >= count and timestamp != packet.timestamp:
                    break
                timestamp = packet.timestamp
            self._packets[pos] = None
            self._origin = uint16_add(self._origin, 1)  # type: ignore[arg-type]
            if i == self._capacity - 1:
                return True
        return False
