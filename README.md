# aiortp

[![CI](https://github.com/anganyAI/aiortp/actions/workflows/ci.yml/badge.svg)](https://github.com/anganyAI/aiortp/actions/workflows/ci.yml)

Asyncio RTP/RTCP library for Python — audio and video.

Plain RTP/RTCP for audio and video — no WebRTC, no ICE, no DTLS. Built for telephony, VoIP, and video streaming applications where you need direct control over RTP streams.

Portions derived from [aiortc](https://github.com/aiortc/aiortc) by Jeremy Lainé (BSD-3-Clause).

## Features

- **Pure Python** — zero required dependencies, Python >=3.11
- **AsyncIO native** — built on `asyncio.DatagramProtocol`
- **Audio codecs** — G.711 µ-law/A-law, L16, G.722 (`pip install aiortp[g722]`), Opus (`pip install aiortp[opus]`)
- **Video codecs** — H.264 (RFC 6184), VP8 (RFC 7741), VP9 (RFC 9628) depacketization/packetization
- **RTCP** — Sender Reports (with real RTP timestamps), Receiver Reports, SDES, BYE, PLI, NACK (retransmission on by default for video, opt-in for audio via `nack_retransmit=True`)
- **DTMF** — RFC 4733 telephone-event send/receive with redundant end packets
- **Jitter buffer** — reordering for both audio (timestamp boundaries) and video (marker-bit frame detection)
- **Adaptive clocked playout** — opt-in `playout=True`: audio delivered on a steady 20 ms media clock from an adaptive jitter buffer that follows measured network jitter
- **Paced sending** — opt-in `paced=True`: outgoing frames queued and transmitted one per ptime, silence encoded as timestamp jumps, `await session.drain()`
- **Packet loss concealment** — confirmed-lost audio replaced with native Opus PLC or generic fade-out repetition, keeping the delivered stream temporally continuous
- **Comfort noise (RFC 3389)** — opt-in `cn=True` emission during paced silence (level measured from the stream); PT 13 reception always handled — playout generates the noise, never reads it as loss
- **Auto-timestamps** — optional automatic RTP timestamp generation for audio and video
- **Port allocation** — `PortAllocator` for managed even/odd RTP/RTCP port pairs
- **STUN** — inline Binding Responses (IPv4/IPv6, no MESSAGE-INTEGRITY) for simple connectivity probes — not a full ICE agent
- **Symmetric RTP** — opt-in remote address latching from inbound packets (RFC 4961) for NAT traversal
- **Fully typed** — PEP 561 `py.typed` marker included

## Installation

```bash
pip install aiortp
```

With optional codecs:

```bash
pip install aiortp[opus]   # Opus support
pip install aiortp[g722]   # G.722 wideband
```

## Quick Start — Audio

```python
import asyncio
from aiortp import RTPSession, PayloadType

async def main():
    session_a = await RTPSession.create(
        local_addr=("127.0.0.1", 10000),
        remote_addr=("127.0.0.1", 10002),
        payload_type=PayloadType.PCMU,
    )

    session_b = await RTPSession.create(
        local_addr=("127.0.0.1", 10002),
        remote_addr=("127.0.0.1", 10000),
        payload_type=PayloadType.PCMU,
    )

    def on_audio(data: bytes, timestamp: int) -> None:
        print(f"Received {len(data)} bytes, ts={timestamp}")

    session_b.on_audio = on_audio

    # Send with auto-incrementing timestamps (160 samples/frame for PCMU)
    pcm = b"\x00" * 320  # 160 samples of silence (20ms at 8kHz)
    for i in range(10):
        session_a.send_audio_pcm_auto(pcm)

    await asyncio.sleep(1)
    await session_a.close()
    await session_b.close()

asyncio.run(main())
```

## Quick Start — Video

```python
import asyncio
from aiortp import VideoRTPSession

async def main():
    sender = await VideoRTPSession.create(
        local_addr=("127.0.0.1", 20000),
        remote_addr=("127.0.0.1", 20002),
        codec="h264",  # also "vp8" or "vp9"
        fps=30,
    )

    receiver = await VideoRTPSession.create(
        local_addr=("127.0.0.1", 20002),
        remote_addr=("127.0.0.1", 20000),
        codec="h264",
    )

    def on_frame(data: bytes, timestamp: int, is_keyframe: bool) -> None:
        print(f"Frame: {len(data)} bytes, keyframe={is_keyframe}")

    receiver.on_frame = on_frame

    # Send H.264 NAL units with auto-incrementing timestamps
    sps = bytes([0x67, 0x42, 0x00, 0x1E])
    pps = bytes([0x68, 0xCE, 0x38, 0x80])
    idr = bytes([0x65]) + b"\x00" * 100
    sender.send_frame_auto([sps, pps, idr], keyframe=True)

    await asyncio.sleep(1)
    await sender.close()
    await receiver.close()

asyncio.run(main())
```

## DTMF

```python
# Send
session.send_dtmf("1", duration_ms=160, timestamp=0)

# Receive
def on_dtmf(digit: str, duration: int) -> None:
    print(f"Got DTMF: {digit}")

session.on_dtmf = on_dtmf
```

## Packet Loss Concealment

With `skip_audio_gaps=True`, the jitter buffer confirms losses by sequence-number
analysis (sender pauses such as DTMF or VAD suppression are never treated as loss).
Confirmed-lost packets are replaced with concealment PCM before `on_audio`, so the
delivered stream stays temporally continuous — recordings and AEC alignment are
preserved. Opus uses native libopus PLC; other codecs fall back to a generic
concealer (last-frame repetition fading to silence over 60 ms, then silence).

```python
session = await RTPSession.create(
    local_addr=("0.0.0.0", 10000),
    remote_addr=("10.0.0.1", 10000),
    payload_type=PayloadType.PCMU,
    skip_audio_gaps=True,  # required: loss is confirmed by the jitter buffer
    plc=True,              # default — set False to skip lost audio silently
)

print(session.stats["concealed_frames"])  # packets replaced by concealment
```

## Clocked Playout & Paced Sending

With `playout=True`, `on_audio` fires on a steady ptime clock (20 ms ticks)
instead of on packet arrival. Frames wait in an adaptive playout buffer whose
target depth follows measured network jitter (bounded by
`playout_max_delay_ms`, default 200 ms): sustained jitter grows the buffer by
inserting a concealment frame, calm networks shrink it by dropping one.
Missing frames are concealed at their deadline (native Opus PLC or fade-out);
after 120 ms of continuous concealment the gap is treated as a sender pause
(DTX, hold, DTMF) and delivery suspends until the stream resumes.

With `paced=True`, `send_audio_auto` / `send_audio_pcm_auto` enqueue frames
and the session transmits one per ptime on its media clock — push faster than
real time (e.g. a whole file) and the wire stays correctly paced. Silence is
a timestamp jump, not stale packets.

```python
session = await RTPSession.create(
    local_addr=("0.0.0.0", 10000),
    remote_addr=("10.0.0.1", 10000),
    payload_type=PayloadType.PCMU,
    playout=True,   # clocked receive: on_audio every 20 ms
    paced=True,     # clocked send: one frame per 20 ms
)

session.on_audio = lambda pcm, ts: sink.write(pcm)

for frame in pcm_frames:          # any rate — even all at once
    session.send_audio_pcm_auto(frame)
await session.drain()             # wait until everything is on the wire

print(session.stats["playout_delay_ms"], session.stats["playout_target_ms"])
```

### Comfort noise (RFC 3389)

With `cn=True` (requires `paced=True`), entering a silence period — the
send queue running empty after talk — emits a comfort-noise packet whose
level is measured from the outgoing stream, refreshed every 3 s while the
silence lasts. The first audio frame after silence carries the RFC 3551
talkspurt marker.

Reception is always on, independent of `cn`: PT 13 packets (configurable
via `cn_payload_type`) consume sequence numbers without ever reading as
packet loss. In playout mode the silence is filled with generated noise at
the signalled level from the CN timestamp onward (`cn_frames` in stats),
falling back to suspension if the sender disappears for 30 s; in
arrival-driven mode the level is surfaced through `on_cn(level)`.

`OpusCodec(dtx=True)` additionally enables the encoder's DTX flag —
whether libopus emits DTX frames depends on its mode selection; aiortp's
silence suppression works through paced mode + CN regardless.

### Timing characteristics

Media clocks schedule against absolute deadlines on the event loop, so
pacing and playout never accumulate drift: a delayed tick catches up
instead of shifting the stream. Indicative measurements (20 ms ticks over
5 s, CPython 3.12, Apple Silicon): mean tick deviation ~0.5 ms and < 0.1 ms
cumulative drift on an idle loop; under a hostile load that holds the loop
in 5 ms chunks, per-tick deviation rises to ~10 ms while cumulative drift
stays below 8 ms and no tick is lost.

Per-tick precision is bounded by event-loop responsiveness: anything that
blocks the loop for N ms delays ticks by up to N ms. Keep `on_audio`
callbacks light, offload heavy work, and consider uvloop for busy
applications. Sender-side pacing jitter is absorbed by the receiver's
jitter buffer like any other network jitter.

## Video RTCP Feedback

```python
# Request a keyframe from the remote sender
receiver.request_keyframe()

# Get notified when the remote side requests a keyframe
def on_keyframe_needed() -> None:
    print("Remote requested a keyframe")

sender.on_keyframe_needed = on_keyframe_needed
```

## Port Allocator

```python
from aiortp import PortAllocator, RTPSession

allocator = PortAllocator(port_range=(10000, 20000))

# Session will use an even/odd port pair from the allocator
session = await RTPSession.create(
    local_addr=("0.0.0.0", 0),
    remote_addr=("10.0.0.1", 10000),
    payload_type=0,
    port_allocator=allocator,
)
# Ports are released automatically on close
```

## Codec Registry

```python
from aiortp import get_codec, PayloadType

codec = get_codec(PayloadType.PCMU)  # or PCMA, L16, G722
encoded = codec.encode(pcm_bytes)
decoded = codec.decode(encoded)
```

## Low-Level Packets

```python
from aiortp import RtpPacket, RtcpPacket, is_rtcp

# Parse
packet = RtpPacket.parse(data)
print(packet.sequence_number, packet.timestamp, packet.payload_type)

# Build
packet = RtpPacket(
    payload_type=0,
    sequence_number=1000,
    timestamp=8000,
    ssrc=0xDEADBEEF,
    payload=b"\x80" * 160,
)
data = packet.serialize()

# Demux RTP vs RTCP
if is_rtcp(data):
    rtcp_packets = RtcpPacket.parse(data)
```

## Video Depacketizers (Standalone)

```python
from aiortp import H264Depacketizer, VP8Depacketizer, VP9Depacketizer

# H.264: feed RTP payloads, get NAL units
depkt = H264Depacketizer()
nals = depkt.feed(rtp_payload, marker=is_last_packet)

# VP8/VP9: feed RTP payloads, get (frame_data, is_keyframe) tuples
depkt = VP8Depacketizer()
frames = depkt.feed(rtp_payload, marker=is_last_packet)
```

## Examples

See the [`examples/`](examples/) directory:

- **`loopback.py`** — two sessions exchanging G.711 audio on localhost
- **`dtmf.py`** — sending and receiving DTMF digits
- **`codec_roundtrip.py`** — encode/decode with each built-in codec
- **`raw_packets.py`** — low-level RTP/RTCP packet construction
- **`send_wav.py`** — stream a WAV file over RTP

## License

MIT. See [LICENSE](LICENSE) for details.
