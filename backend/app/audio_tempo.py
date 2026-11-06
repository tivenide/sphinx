"""Pitch-preserving PCM tempo for streamed speech.

Only the streaming path is retained. Every consumer must close its iterator on
abort; that cancels the HTTP feeder and reaps the per-response FFmpeg process.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, AsyncIterator


class AudioTempoError(RuntimeError):
    """The bounded FFmpeg process could not transform the audio."""


async def tempo_pcm_stream(
    chunks: AsyncIterable[bytes], *, sample_rate: int, rate: float
) -> AsyncIterator[bytes]:
    """Stream mono PCM16 through FFmpeg's atempo filter."""
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or not 8_000 <= sample_rate <= 48_000:
        raise ValueError("Unsupported sample rate")
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or not 0.5 <= rate <= 2.0:
        raise ValueError("Speech rate must be between 0.5 and 2.0")
    if rate == 1.0:
        async for chunk in chunks:
            if chunk:
                yield chunk
        return

    process = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-probesize", "32",
        "-f", "s16le", "-ar", str(sample_rate), "-ac", "1", "-i", "pipe:0",
        "-filter:a", f"atempo={rate:.6f}", "-f", "s16le", "-acodec", "pcm_s16le", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None

    async def feed() -> None:
        try:
            async for chunk in chunks:
                if chunk:
                    process.stdin.write(chunk)
                    await process.stdin.drain()
        finally:
            process.stdin.close()

    async def drain_stderr() -> None:
        # Drain without retaining content-bearing/unbounded diagnostic output.
        while await process.stderr.read(4096):
            pass

    feeder = asyncio.create_task(feed(), name="sphinx-tts-tempo-input")
    stderr_reader = asyncio.create_task(drain_stderr(), name="sphinx-tts-tempo-stderr")
    try:
        while chunk := await process.stdout.read(16 * 1024):
            yield chunk
        await feeder
        await stderr_reader
        if await process.wait() != 0:
            raise AudioTempoError("FFmpeg tempo adjustment failed")
    finally:
        feeder.cancel()
        await asyncio.gather(feeder, return_exceptions=True)
        if process.returncode is None:
            process.kill()
            await process.wait()
        stderr_reader.cancel()
        await asyncio.gather(stderr_reader, return_exceptions=True)
