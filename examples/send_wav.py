#!/usr/bin/env python3
"""
Send a WAV file over RTP to a specific address and port.

Usage:
    python examples/send_wav.py <wav_file> <dest_host> <dest_port>

Examples:
    python examples/send_wav.py audio.wav 192.168.1.100 5004
    python examples/send_wav.py prompt.wav 10.0.0.5 8000

The WAV file must be:
  - 16-bit signed PCM (s16le)
  - Mono (1 channel)
  - 8000 Hz sample rate

The audio is encoded with G.711 µ-law (PCMU, payload type 0) and sent
as 20 ms RTP frames (160 samples each) with proper real-time pacing.
"""

import argparse
import asyncio
import struct
import sys
import wave

from aiortp import RTPSession, PayloadType


async def send_wav(wav_path: str, dest_host: str, dest_port: int) -> None:
    # Read and validate WAV file
    with wave.open(wav_path, "rb") as wf:
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        n_frames = wf.getnframes()
        pcm_data = wf.readframes(n_frames)

    if channels != 1:
        print(f"Error: WAV must be mono (got {channels} channels)", file=sys.stderr)
        sys.exit(1)
    if sample_width != 2:
        print(
            f"Error: WAV must be 16-bit PCM (got {sample_width * 8}-bit)",
            file=sys.stderr,
        )
        sys.exit(1)
    if sample_rate != 8000:
        print(
            f"Error: WAV must be 8000 Hz (got {sample_rate} Hz)", file=sys.stderr
        )
        sys.exit(1)

    duration_sec = n_frames / sample_rate
    print(f"File: {wav_path}")
    print(f"  Format: {sample_rate} Hz, 16-bit mono PCM")
    print(f"  Duration: {duration_sec:.2f}s ({n_frames} samples)")

    # Create RTP session
    session = await RTPSession.create(
        local_addr=("0.0.0.0", 0),
        remote_addr=(dest_host, dest_port),
        payload_type=PayloadType.PCMU,
    )

    local_addr = session._rtp_transport._transport.get_extra_info("sockname")
    print(f"  Sending from {local_addr[0]}:{local_addr[1]}")
    print(f"  Sending to   {dest_host}:{dest_port}")
    print()

    # Send audio in 20 ms frames (160 samples = 320 bytes of s16le PCM)
    samples_per_frame = 160
    frame_size = samples_per_frame * 2  # 2 bytes per sample (s16le)
    frame_duration = samples_per_frame / sample_rate  # 0.02s = 20ms
    total_frames = len(pcm_data) // frame_size
    timestamp = 0

    print(f"Streaming {total_frames} frames ({frame_duration * 1000:.0f} ms each) ...")

    for i in range(total_frames):
        offset = i * frame_size
        frame_pcm = pcm_data[offset : offset + frame_size]

        session.send_audio_pcm(frame_pcm, timestamp=timestamp)
        timestamp += samples_per_frame

        # Real-time pacing: sleep ~20 ms between frames
        await asyncio.sleep(frame_duration)

        # Progress indicator every second
        if (i + 1) % 50 == 0:
            elapsed = (i + 1) * frame_duration
            print(f"  Sent {elapsed:.1f}s / {duration_sec:.1f}s")

    # Handle remaining samples (partial last frame, if any)
    remaining = len(pcm_data) - total_frames * frame_size
    if remaining >= 2:
        # Pad to a full frame with silence
        last_pcm = pcm_data[total_frames * frame_size :]
        last_pcm += b"\x00" * (frame_size - remaining)
        session.send_audio_pcm(last_pcm, timestamp=timestamp)

    print(f"\nDone! Sent {total_frames} frames ({duration_sec:.2f}s of audio).")
    print(f"Stats: {session.stats}")

    await session.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send a WAV file over RTP (G.711 µ-law)"
    )
    parser.add_argument("wav_file", help="Path to the WAV file (8 kHz, 16-bit, mono)")
    parser.add_argument("dest_host", help="Destination IP address")
    parser.add_argument("dest_port", type=int, help="Destination RTP port")
    args = parser.parse_args()

    asyncio.run(send_wav(args.wav_file, args.dest_host, args.dest_port))


if __name__ == "__main__":
    main()
