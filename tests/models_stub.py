"""Deterministic model protocol fixture. Used ONLY by tests/compose.yaml, never production."""

import asyncio
import json
import math
import struct
import time
import hmac
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse

app = FastAPI()
stats = {"asr": 0, "llm": 0, "tts": 0, "loads": 0, "history_messages": []}
ready_at = None


@app.get("/ready")
@app.get("/health")
@app.get("/status")
async def ready():
    loaded = ready_at is not None and time.monotonic() >= ready_at
    return JSONResponse({"status": "ready" if loaded else "loading"}, status_code=200 if loaded else 503)


def control_status():
    state = "not_loaded" if ready_at is None else "loading" if time.monotonic() < ready_at else "ready"
    return {"state": state, "can_load": state == "not_loaded", "detail": None,
            "models": [{"id": key, "label": label, "state": state, "detail": None}
                       for key, label in [("asr", "Spracherkennung"), ("llm", "Antwortmodell"), ("tts", "Sprachausgabe")]]}


def authorize(request):
    token = Path('/run/secrets/model_control_token').read_text().strip()
    if not hmac.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + token):
        raise HTTPException(401)


@app.get("/control/status")
async def models_status(request: Request):
    authorize(request)
    return control_status()


@app.post("/control/load")
async def load_models(request: Request):
    global ready_at
    authorize(request)
    assert await request.body() == b""
    if ready_at is None:
        ready_at = time.monotonic() + 3
        stats['loads'] += 1
    return JSONResponse(control_status(), status_code=202)


@app.get("/test/stats")
async def get_stats():
    return stats


@app.post("/v1/audio/transcriptions")
async def transcribe(request: Request):
    data = await request.body()
    assert b"RIFF" in data and b"WAVE" in data  # Internal ASR adapter.
    stats["asr"] += 1
    return {"text": "Das ist eine simulierte Testaufnahme."}


@app.post("/v1/chat/completions")
async def reply(request: Request):
    payload = await request.json()
    stats["llm"] += 1
    # Count completed dialogue pairs, excluding system and retrieval context.
    messages = payload["messages"]
    stats["history_messages"].append(2 * sum(
        previous["role"] == "user" and current["role"] == "assistant"
        for previous, current in zip(messages, messages[1:])
    ))

    async def stream():
        for text in ["Die Verbindung ", "für unsere Sprachaufnahme ", "funktioniert jetzt."]:
            await asyncio.sleep(0.03)
            yield "data: " + json.dumps({"choices": [{"delta": {"content": text}}]}) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/v1/voice-clone/stream")
async def speech():
    stats["tts"] += 1

    async def stream():
        for offset in range(0, 72000, 2400):
            yield b"".join(struct.pack("<h", int(900 * math.sin(2 * math.pi * 330 * n / 24000)))
                           for n in range(offset, offset + 2400))
            await asyncio.sleep(0.01)

    return StreamingResponse(stream(), media_type="application/octet-stream", headers={"X-Audio-Sample-Rate": "24000"})
