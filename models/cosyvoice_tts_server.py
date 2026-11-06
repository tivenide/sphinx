"""Bounded, startup-loaded CosyVoice TTS sidecar."""

from __future__ import annotations

import io
import logging
import os
import sys
import time
import wave
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from fastapi import FastAPI, Form, HTTPException, status
from fastapi.responses import Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from bounded_inference_actor import (
    ActorClosedError,
    AdmissionError,
    BoundedInferenceActor,
    InferenceHandle,
)
from inference_limits import MAX_TTS_OUTPUT_BYTES, MAX_TTS_TEXT_CODE_POINTS

SERVICE_DIR = Path(__file__).resolve().parent
COSYVOICE_DIR = SERVICE_DIR / "vendor" / "CosyVoice"
MODEL_DIR = Path(
    os.getenv(
        "COSYVOICE_MODEL_DIR",
        "/models/cosyvoice/Fun-CosyVoice3-0.5B",
    )
)
REFERENCE_AUDIO = os.getenv(
    "TTS_REFERENCE_AUDIO",
    os.getenv(
        "SPHINX_TTS_REFERENCE_AUDIO",
        "/app/voice/sphinx-male-calm.wav",
    ),
)
REFERENCE_TEXT = os.getenv(
    "TTS_REFERENCE_TEXT",
    os.getenv(
        "SPHINX_TTS_REFERENCE_TEXT",
        "Hallo, ich bin Sphinx. Ich höre dir zu, ordne deine Anfrage ein und antworte klar "
        "und verständlich. Du kannst ganz normal mit mir sprechen.",
    ),
)
VOICE_ID = "sphinx-male-calm"
DEFAULT_SEED = int(os.getenv("COSYVOICE_SEED", "1986"))
QUEUE_FULL_CODE = "sphinx_tts_queue_full"
UNAVAILABLE_CODE = "sphinx_tts_unavailable"
TEXT_TOO_LARGE_CODE = "sphinx_tts_text_too_large"
OUTPUT_TOO_LARGE_CODE = "sphinx_tts_output_too_large"
ACTOR_SHUTDOWN_TIMEOUT_SECONDS = 5.0

logger = logging.getLogger("sphinx.cosyvoice")


def _configure_content_safe_logging() -> None:
    """Suppress content-bearing vendor logs while retaining bounded service metadata."""

    logging.getLogger().setLevel(logging.CRITICAL + 1)
    logger.setLevel(logging.INFO)


class TtsOutputTooLargeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CosyVoiceInferenceRequest:
    text: str
    seed: int
    stream: bool


class CosyVoiceWorker(Protocol):
    def __call__(self, request: CosyVoiceInferenceRequest) -> Iterator[bytes]: ...


@dataclass(frozen=True, slots=True)
class CosyVoiceRuntime:
    worker: CosyVoiceWorker
    sample_rate: int


CosyVoiceRuntimeLoader = Callable[[], CosyVoiceRuntime]


def load_default_runtime() -> CosyVoiceRuntime:
    """Load and prepare all vendor/CUDA state during application startup."""

    sys.path.insert(0, str(COSYVOICE_DIR))
    sys.path.insert(0, str(COSYVOICE_DIR / "third_party" / "Matcha-TTS"))

    import numpy as np
    from cosyvoice.cli.cosyvoice import AutoModel
    from cosyvoice.utils.common import set_all_random_seed

    if not MODEL_DIR.is_dir():
        raise RuntimeError(f"CosyVoice-Modell fehlt: {MODEL_DIR}")
    if not Path(REFERENCE_AUDIO).is_file():
        raise RuntimeError(f"SPHINX-Referenzstimme fehlt: {REFERENCE_AUDIO}")
    if not REFERENCE_TEXT.strip():
        raise RuntimeError("Der exakte Text der SPHINX-Referenzstimme fehlt.")

    model = AutoModel(model_dir=str(MODEL_DIR), fp16=True)
    # This is a dedicated process with ample VRAM. Keeping the allocator warm
    # avoids reallocating model workspaces between short conversation segments.
    if os.getenv("COSYVOICE_KEEP_CUDA_CACHE", "1") == "1":
        import torch

        torch.cuda.empty_cache = lambda: None

    prompt_text = f"You are a helpful assistant.<|endofprompt|>{REFERENCE_TEXT.strip()}"
    if not model.add_zero_shot_spk(prompt_text, REFERENCE_AUDIO, VOICE_ID):
        raise RuntimeError("Das SPHINX-Stimmenprofil konnte nicht vorbereitet werden.")
    _configure_content_safe_logging()

    # Vendor streaming mutates token_hop_len across calls. Restore the pristine
    # value at the beginning of every actor-owned job.
    initial_token_hop_len = getattr(model.model, "token_hop_len", None)
    logger.info(
        "SPHINX voice prepared reference=%s transcript_chars=%d",
        REFERENCE_AUDIO,
        len(REFERENCE_TEXT.strip()),
    )

    def pcm_bytes(speech: object) -> bytes:
        samples = speech.squeeze().detach().cpu().float().numpy()
        samples = np.clip(samples, -1.0, 1.0)
        return (samples * 32767.0).astype("<i2").tobytes()

    def generate(request: CosyVoiceInferenceRequest) -> Iterator[bytes]:
        started_at = time.perf_counter()
        first_chunk_at: float | None = None
        set_all_random_seed(request.seed)
        if initial_token_hop_len is not None:
            model.model.token_hop_len = initial_token_hop_len
        # German normalization happens upstream. The vendored frontend would
        # otherwise apply English number rules and append punctuation.
        output = model.inference_zero_shot(
            request.text,
            "",
            "",
            zero_shot_spk_id=VOICE_ID,
            stream=request.stream,
            text_frontend=False,
        )
        try:
            for chunk in output:
                if first_chunk_at is None:
                    first_chunk_at = time.perf_counter()
                    logger.info(
                        "CosyVoice first audio chars=%d latency=%.3fs",
                        len(request.text),
                        first_chunk_at - started_at,
                    )
                yield pcm_bytes(chunk["tts_speech"])
        except GeneratorExit:
            logger.info(
                "CosyVoice stream aborted by client chars=%d after=%.3fs",
                len(request.text),
                time.perf_counter() - started_at,
            )
            raise
        finally:
            output.close()
        logger.info(
            "CosyVoice completed chars=%d total=%.3fs",
            len(request.text),
            time.perf_counter() - started_at,
        )

    return CosyVoiceRuntime(worker=generate, sample_rate=int(model.sample_rate))


def _wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return output.getvalue()


def _service_unavailable(code: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"code": code},
        headers={"Retry-After": "1"},
    )


def _submit(
    actor: BoundedInferenceActor[CosyVoiceInferenceRequest, None, bytes],
    request: CosyVoiceInferenceRequest,
) -> InferenceHandle[None, bytes]:
    try:
        return actor.submit(request)
    except AdmissionError as exc:
        raise _service_unavailable(QUEUE_FULL_CODE) from exc
    except ActorClosedError as exc:
        raise _service_unavailable(UNAVAILABLE_CODE) from exc


async def _actor_chunks(
    handle: InferenceHandle[None, bytes],
) -> AsyncIterator[bytes]:
    stream = handle.stream()
    try:
        async for chunk in stream:
            yield chunk
    finally:
        # Both explicit ASGI close and task cancellation cross this exact
        # boundary. The actor closes the active vendor generator cooperatively.
        handle.cancel()
        close = getattr(stream, "aclose", None)
        if close is not None:
            await close()


async def _bounded_actor_chunks(
    handle: InferenceHandle[None, bytes],
) -> AsyncIterator[bytes]:
    total = 0
    chunks = _actor_chunks(handle)
    try:
        async for chunk in chunks:
            if len(chunk) > MAX_TTS_OUTPUT_BYTES - total:
                raise TtsOutputTooLargeError
            total += len(chunk)
            yield chunk
    finally:
        await chunks.aclose()


class _OwnedActorStreamingResponse(StreamingResponse):
    """Cancel an admitted Actor job even if ASGI fails before body iteration."""

    def __init__(
        self, *args: object, handle: InferenceHandle[None, bytes], **kwargs: object
    ) -> None:
        super().__init__(*args, **kwargs)
        self._handle = handle

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                close = getattr(self.body_iterator, "aclose", None)
                if close is not None:
                    await close()
            finally:
                self._handle.cancel()


def create_app(
    *,
    runtime_loader: CosyVoiceRuntimeLoader = load_default_runtime,
    max_waiting: int = 4,
) -> FastAPI:
    """Create an injectable CosyVoice app with startup-owned heavy state."""

    @asynccontextmanager
    async def lifespan(service: FastAPI):
        runtime = runtime_loader()
        if (
            isinstance(runtime.sample_rate, bool)
            or not isinstance(runtime.sample_rate, int)
            or runtime.sample_rate < 1
        ):
            raise ValueError("CosyVoice sample_rate must be a positive integer")
        actor = BoundedInferenceActor(
            runtime.worker,
            max_waiting=max_waiting,
            stream_buffer_size=2,
            name="sphinx-cosyvoice-tts",
        )
        service.state.inference_actor = actor
        service.state.inference_runtime = runtime
        service.state.ready = True
        try:
            yield
        finally:
            service.state.ready = False
            await actor.shutdown(timeout=ACTOR_SHUTDOWN_TIMEOUT_SECONDS)

    service = FastAPI(title="SPHINX CosyVoice TTS", lifespan=lifespan)
    service.state.ready = False

    @service.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @service.get("/status", include_in_schema=False)
    async def inference_status() -> dict[str, str | int]:
        actor = getattr(service.state, "inference_actor", None)
        if actor is None:
            return {
                "status": "unavailable",
                "active_requests": 0,
                "waiting_requests": 0,
            }
        snapshot = actor.snapshot()
        ready = service.state.ready and snapshot.accepting and snapshot.worker_alive
        return {
            "status": "ready" if ready else "unavailable",
            "active_requests": snapshot.active,
            "waiting_requests": snapshot.waiting,
        }

    @service.post("/v1/voice-clone/stream")
    async def voice_clone_stream(
        text: str = Form(...),
        language: str = Form("German"),
        seed: int = Form(DEFAULT_SEED),
    ) -> StreamingResponse:
        del language
        normalized_text = text.strip()
        if not normalized_text:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Text darf nicht leer sein.",
            )
        if len(normalized_text) > MAX_TTS_TEXT_CODE_POINTS:
            raise HTTPException(
                status_code=422,
                detail={"code": TEXT_TOO_LARGE_CODE},
            )
        handle = _submit(
            service.state.inference_actor,
            CosyVoiceInferenceRequest(text=normalized_text, seed=seed, stream=True),
        )
        try:
            return _OwnedActorStreamingResponse(
                _bounded_actor_chunks(handle),
                handle=handle,
                media_type="audio/L16",
                headers={
                    "X-Audio-Encoding": "s16le",
                    "X-Audio-Sample-Rate": str(
                        service.state.inference_runtime.sample_rate
                    ),
                    "X-Audio-Channels": "1",
                    "X-TTS-Prompt-Cached": "true",
                },
            )
        except Exception:
            handle.cancel()
            raise

    @service.post("/v1/voice-clone")
    async def voice_clone(
        text: str = Form(...),
        language: str = Form("German"),
        seed: int = Form(DEFAULT_SEED),
    ) -> Response:
        del language
        normalized_text = text.strip()
        if not normalized_text:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Text darf nicht leer sein.",
            )
        if len(normalized_text) > MAX_TTS_TEXT_CODE_POINTS:
            raise HTTPException(
                status_code=422,
                detail={"code": TEXT_TOO_LARGE_CODE},
            )
        handle = _submit(
            service.state.inference_actor,
            CosyVoiceInferenceRequest(text=normalized_text, seed=seed, stream=False),
        )
        started_at = time.perf_counter()
        pcm = bytearray()
        try:
            async for chunk in _bounded_actor_chunks(handle):
                pcm.extend(chunk)
        except TtsOutputTooLargeError as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"code": OUTPUT_TOO_LARGE_CODE},
            ) from exc
        elapsed = time.perf_counter() - started_at
        sample_rate = service.state.inference_runtime.sample_rate
        audio_seconds = len(pcm) / (sample_rate * 2)
        return Response(
            _wav_bytes(pcm, sample_rate),
            media_type="audio/wav",
            headers={
                "X-TTS-Generation-Seconds": f"{elapsed:.3f}",
                "X-TTS-Audio-Seconds": f"{audio_seconds:.3f}",
                "X-TTS-Prompt-Cached": "true",
            },
        )

    return service


app = create_app()
