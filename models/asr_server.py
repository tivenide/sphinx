"""Bounded, startup-loaded Qwen ASR sidecar."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, File, Form, HTTPException, UploadFile, status

from bounded_inference_actor import (
    ActorClosedError,
    AdmissionError,
    BoundedInferenceActor,
    InferenceHandle,
    ShutdownTimeoutError,
)
from inference_limits import MAX_ASR_AUDIO_BYTES

logger = logging.getLogger(__name__)

MODEL_NAME = os.getenv("ASR_MODEL", "Qwen/Qwen3-ASR-0.6B")
MODEL_REVISION = os.getenv("ASR_MODEL_REVISION") or {
    "Qwen/Qwen3-ASR-0.6B": "5eb144179a02acc5e5ba31e748d22b0cf3e303b0",
    "Qwen/Qwen3-ASR-1.7B": "7278e1e70fe206f11671096ffdd38061171dd6e5",
}.get(MODEL_NAME)
QUEUE_FULL_CODE = "sphinx_asr_queue_full"
UNAVAILABLE_CODE = "sphinx_asr_unavailable"
INPUT_TOO_LARGE_CODE = "sphinx_asr_audio_too_large"
ACTOR_SHUTDOWN_TIMEOUT_SECONDS = 5.0
ASR_WARMUP_TIMEOUT_SECONDS = 225.0
ASR_WARMUP_AUDIO = Path(__file__).with_name("voice") / "sphinx-male-calm.wav"


@dataclass(frozen=True, slots=True)
class AsrInferenceRequest:
    audio: bytes
    suffix: str
    language: str


@dataclass(frozen=True, slots=True)
class AsrInferenceResult:
    text: str
    language: str | None


class AsrWorker(Protocol):
    def __call__(self, request: AsrInferenceRequest) -> AsrInferenceResult: ...


AsrWorkerLoader = Callable[[], AsrWorker]


def create_file_worker(model: Any) -> AsrWorker:
    """Adapt a Qwen ASR model to the bounded file-request worker."""

    def transcribe(request: AsrInferenceRequest) -> AsrInferenceResult:
        # Request tempfiles belong to the active actor job, never to queued HTTP
        # handlers. This bounds their lifetime and keeps every exit cleanup-local.
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                suffix=request.suffix, delete=False
            ) as temp_file:
                temp_path = Path(temp_file.name)
                temp_file.write(request.audio)
            qwen_language = (
                "German"
                if request.language.lower() in {"de", "german", "deutsch"}
                else None
            )
            result = model.transcribe(audio=str(temp_path), language=qwen_language)[0]
            return AsrInferenceResult(text=result.text, language=result.language)
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    return transcribe


def _warmup_request() -> AsrInferenceRequest:
    return AsrInferenceRequest(
        audio=ASR_WARMUP_AUDIO.read_bytes(),
        suffix=".wav",
        language="de",
    )


def _torch_peak_reserved_mib() -> int | None:
    try:
        import torch

        return round(torch.cuda.max_memory_reserved() / 1024**2)
    except Exception:
        return None


def load_default_worker() -> AsrWorker:
    """Load CUDA/model dependencies only during application startup."""

    import torch
    from huggingface_hub import snapshot_download
    from qwen_asr import Qwen3ASRModel

    if MODEL_REVISION is None:
        raise RuntimeError("ASR_MODEL_REVISION is required for an unpinned ASR model")
    model_path = snapshot_download(repo_id=MODEL_NAME, revision=MODEL_REVISION)
    model = Qwen3ASRModel.from_pretrained(
        model_path,
        dtype=torch.bfloat16,
        device_map=os.getenv("ASR_DEVICE", "cuda:0"),
        max_inference_batch_size=8,
        max_new_tokens=512,
    )
    return create_file_worker(model)


def _service_unavailable(code: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={"code": code},
        headers={"Retry-After": "1"},
    )


def _submit(
    actor: BoundedInferenceActor[AsrInferenceRequest, AsrInferenceResult, object],
    request: AsrInferenceRequest,
) -> InferenceHandle[AsrInferenceResult, object]:
    try:
        return actor.submit(request)
    except AdmissionError as exc:
        raise _service_unavailable(QUEUE_FULL_CODE) from exc
    except ActorClosedError as exc:
        raise _service_unavailable(UNAVAILABLE_CODE) from exc


async def _result(
    handle: InferenceHandle[AsrInferenceResult, object],
) -> AsrInferenceResult:
    try:
        return await handle.result()
    except asyncio.CancelledError:
        handle.cancel()
        raise


def create_app(
    *,
    worker_loader: AsrWorkerLoader = load_default_worker,
    max_waiting: int = 4,
    warmup_timeout_seconds: float = ASR_WARMUP_TIMEOUT_SECONDS,
) -> FastAPI:
    """Create an injectable ASR app without importing or loading CUDA at import time."""

    @asynccontextmanager
    async def lifespan(service: FastAPI):
        worker: AsrWorker | None = None

        def infer(request: AsrInferenceRequest) -> AsrInferenceResult:
            nonlocal worker
            if worker is None:
                worker = worker_loader()
            return worker(request)

        actor = BoundedInferenceActor(
            infer,
            max_waiting=max_waiting,
            stream_buffer_size=2,
            name="sphinx-asr",
        )
        service.state.inference_actor = actor
        service.state.ready = False
        started = time.monotonic()
        try:
            await asyncio.wait_for(
                _result(_submit(actor, _warmup_request())),
                timeout=warmup_timeout_seconds,
            )
        except BaseException:
            try:
                await actor.shutdown(timeout=0)
            except ShutdownTimeoutError:
                pass
            raise
        logger.info(
            "event=sphinx_asr_warmup duration_seconds=%.3f torch_peak_reserved_mib=%s",
            time.monotonic() - started,
            _torch_peak_reserved_mib(),
        )
        service.state.ready = True
        try:
            yield
        finally:
            service.state.ready = False
            await actor.shutdown(timeout=ACTOR_SHUTDOWN_TIMEOUT_SECONDS)

    service = FastAPI(title="Sphinx Qwen3 ASR", lifespan=lifespan)
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

    @service.get("/ready", include_in_schema=False)
    async def ready() -> dict[str, str]:
        if not service.state.ready:
            raise _service_unavailable(UNAVAILABLE_CODE)
        return {"status": "ready"}

    @service.post("/v1/audio/transcriptions")
    async def transcribe(
        file: UploadFile = File(...),
        model_name: str = Form(MODEL_NAME, alias="model"),
        language: str = Form("de"),
    ) -> dict[str, str | None]:
        del model_name
        if not service.state.ready:
            raise _service_unavailable(UNAVAILABLE_CODE)
        suffix = Path(file.filename or "audio.webm").suffix or ".webm"
        try:
            audio = await file.read(MAX_ASR_AUDIO_BYTES + 1)
        finally:
            await file.close()
        if len(audio) > MAX_ASR_AUDIO_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail={"code": INPUT_TOO_LARGE_CODE},
            )
        inference_request = AsrInferenceRequest(
            audio=audio,
            suffix=suffix,
            language=language,
        )
        actor = service.state.inference_actor
        result = await _result(_submit(actor, inference_request))
        return {"text": result.text, "language": result.language}

    return service


app = create_app()
