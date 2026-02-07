"""
DTMF example: send and receive telephone-event digits between two sessions.

Demonstrates:
  - Sending DTMF digits via session.send_dtmf()
  - Receiving DTMF via the on_dtmf callback
"""

import asyncio

from aiortp import RTPSession, PayloadType


async def main() -> None:
    received_digits: list[tuple[str, int]] = []
    done = asyncio.Event()

    # --- Set up two sessions ---
    sender = await RTPSession.create(
        local_addr=("127.0.0.1", 0),
        remote_addr=("127.0.0.1", 0),
        payload_type=PayloadType.PCMU,
        dtmf_payload_type=101,
    )

    sender_addr = sender._rtp_transport._transport.get_extra_info("sockname")

    receiver = await RTPSession.create(
        local_addr=("127.0.0.1", 0),
        remote_addr=sender_addr,
        payload_type=PayloadType.PCMU,
        dtmf_payload_type=101,
    )

    receiver_addr = receiver._rtp_transport._transport.get_extra_info("sockname")
    sender.update_remote(receiver_addr)

    # --- Register DTMF callback ---
    def on_dtmf(digit: str, duration: int) -> None:
        received_digits.append((digit, duration))
        print(f"  Received DTMF: digit='{digit}', duration={duration} samples")
        if len(received_digits) >= 4:
            done.set()

    receiver.on_dtmf = on_dtmf

    # --- Send digits 1, 2, 3, # ---
    # Each digit gets a unique timestamp so the receiver can distinguish them.
    digits = ["1", "2", "3", "#"]
    print(f"Sending DTMF digits: {' '.join(digits)}")
    for i, digit in enumerate(digits):
        sender.send_dtmf(digit, duration_ms=160, timestamp=i * 1600)
        await asyncio.sleep(0.05)  # small gap between digits

    # Wait for reception
    try:
        await asyncio.wait_for(done.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        pass

    print(f"\nDigits received: {[d for d, _ in received_digits]}")

    await sender.close()
    await receiver.close()


if __name__ == "__main__":
    asyncio.run(main())
