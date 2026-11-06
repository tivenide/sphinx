"""Focused model-free checks for the ASR and TTS services."""

from __future__ import annotations

import os
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import wave

from fastapi.testclient import TestClient
import pytest

import asr_server
import cosyvoice_tts_server
from inference_limits import MAX_ASR_AUDIO_BYTES, MAX_TTS_TEXT_CODE_POINTS


@pytest.mark.parametrize("module_name", ["asr_server", "cosyvoice_tts_server"])
def test_import_does_not_load_model_dependencies(module_name: str) -> None:
    script = f"""
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {{'torch', 'qwen_asr', 'cosyvoice', 'numpy'}}:
        raise AssertionError('Model dependency imported before startup')
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
assert __import__({module_name!r}).app is not None
"""
    subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        check=True,
        capture_output=True,
    )


def test_asr_ready_and_bounded_internal_audio() -> None:
    requests = []

    def worker(request):
        requests.append(request)
        return asr_server.AsrInferenceResult(text="Guten Tag", language="German")

    with TestClient(asr_server.create_app(worker_loader=lambda: worker)) as client:
        assert client.get("/ready").status_code == 200
        response = client.post(
            "/v1/audio/transcriptions",
            files={"file": ("turn.wav", b"in-memory-turn", "audio/wav")},
            data={"language": "de"},
        )
        assert response.json() == {"text": "Guten Tag", "language": "German"}
        too_large = client.post(
            "/v1/audio/transcriptions",
            files={"file": ("turn.wav", b"x" * (MAX_ASR_AUDIO_BYTES + 1), "audio/wav")},
        )
        assert too_large.status_code == 413
    # The first inference warms the model, the second handles the utterance.
    assert len(requests) == 2
    reference = Path(__file__).resolve().parents[1] / "voice" / "sphinx-male-calm.wav"
    assert requests[0].audio == reference.read_bytes()
    assert requests[0].suffix == ".wav"
    assert requests[0].language == "de"
    assert requests[1].audio == b"in-memory-turn"


def test_asr_tempfile_is_removed_after_failure() -> None:
    paths = []

    class Model:
        def transcribe(self, *, audio, language):
            path = Path(audio)
            assert path.read_bytes() == b"ephemeral-audio"
            assert language == "German"
            paths.append(path)
            raise RuntimeError("inference failed")

    worker = asr_server.create_file_worker(Model())
    with pytest.raises(RuntimeError, match="inference failed"):
        worker(asr_server.AsrInferenceRequest(b"ephemeral-audio", ".wav", "de"))
    assert paths and not paths[0].exists()


def test_tts_stream_contract_and_input_limits() -> None:
    requests = []

    def worker(request):
        requests.append(request)
        yield b"\x00\x00\x01\x00"
        yield b"\x02\x00"

    runtime = cosyvoice_tts_server.CosyVoiceRuntime(worker=worker, sample_rate=24000)
    with TestClient(cosyvoice_tts_server.create_app(runtime_loader=lambda: runtime)) as client:
        assert client.get("/status").json()["status"] == "ready"
        response = client.post("/v1/voice-clone/stream", data={"text": "Guten Tag."})
        assert response.status_code == 200
        assert response.headers["x-audio-encoding"] == "s16le"
        assert response.headers["x-audio-sample-rate"] == "24000"
        assert response.content == b"\x00\x00\x01\x00\x02\x00"
        assert client.post("/v1/voice-clone/stream", data={"text": " "}).status_code == 400
        assert client.post(
            "/v1/voice-clone/stream", data={"text": "x" * (MAX_TTS_TEXT_CODE_POINTS + 1)}
        ).status_code == 422
    assert len(requests) == 1
    assert requests[0].stream is True


def test_reference_voice_has_matching_nonempty_transcript() -> None:
    voice_dir = Path(__file__).resolve().parents[1] / "voice"
    reference = voice_dir / "sphinx-male-calm.wav"
    text = reference.with_suffix(".txt").read_text().strip()
    assert text
    with wave.open(str(reference)) as audio:
        assert audio.getnchannels() == 1
        assert audio.getsampwidth() == 2
        assert audio.getframerate() == 24000
        assert 3 < audio.getnframes() / audio.getframerate() < 30
    metadata = json.loads(reference.with_suffix(".json").read_text())
    assert metadata["text"] == text
    assert metadata["sha256"] == hashlib.sha256(reference.read_bytes()).hexdigest()
    assert metadata["offline"] is True
