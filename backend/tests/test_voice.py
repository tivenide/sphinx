"""Deterministic session-validation timing, without models or real-time sleeps."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from app import voice
from app.security import COOKIE_NAME


class Clock:
    now = 0.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1000 + self.now

    def advance(self, seconds):
        self.now += seconds


class Database:
    def __init__(self, clock):
        self.clock = clock
        self.calls = 0
        self.revoked = False
        self.session_data = {"id": 1, "token_hash": "session-hash", "expires_at": 2000.0}

    def session(self, token):
        assert token == "session-token"
        self.calls += 1
        if self.revoked or self.clock.time() >= self.session_data["expires_at"]:
            return None
        return self.session_data.copy()


class Pipeline:
    def __init__(self):
        self.calls = 0
        self.wait = False
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def run(self, pcm, history):
        self.calls += 1
        self.started.set()
        try:
            if self.wait:
                await asyncio.Event().wait()
            yield {"type": "result", "transcript": "Frage", "reply": "Antwort"}
        finally:
            self.closed.set()


class Socket:
    headers = {"origin": "https://localhost:6500"}
    cookies = {COOKIE_NAME: "session-token"}

    def __init__(self, incoming):
        self.incoming = incoming
        self.sent = []
        self.done = asyncio.Event()
        self.close_code = None

    async def accept(self):
        pass

    async def send_json(self, event):
        self.sent.append(event)
        if event["type"] == "done":
            self.done.set()

    async def close(self, code):
        if self.close_code is None:
            self.close_code = code

    async def receive(self):
        try:
            event = await anext(self.incoming)
        except StopAsyncIteration:
            return {"type": "websocket.disconnect"}
        if isinstance(event, Exception):
            raise event
        return event


def frame():
    return {"type": "websocket.receive", "bytes": b"\x00\x00" * 512}


def control(kind):
    return {"type": "websocket.receive", "text": json.dumps({"type": kind})}


@pytest.fixture
def runtime(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(voice, "time", clock)
    db = Database(clock)
    pipeline = Pipeline()
    settings = SimpleNamespace(allowed_origins={"https://localhost:6500"}, max_record_seconds=30, request_timeout=180)
    return SimpleNamespace(clock=clock, db=db, pipeline=pipeline, app=voice.VoiceRuntime(db, settings, pipeline))


@pytest.mark.asyncio
async def test_many_audio_frames_reuse_validation_until_turn_boundaries(runtime):
    async def incoming():
        for _ in range(90):
            runtime.clock.advance(0.01)
            yield frame()
        assert runtime.db.calls == 1  # Only the handshake, not ninety SQLite reads.
        yield control("finish")
        await socket.done.wait()
        assert runtime.db.calls == 2  # Fresh validation before inference.
        yield control("played")

    socket = Socket(incoming())
    await asyncio.wait_for(runtime.app.handle(socket), 2)
    assert runtime.db.calls == 3  # Fresh validation before recording completed history.
    assert runtime.pipeline.calls == 1
    assert socket.close_code == 1000
    assert len(runtime.app.histories["session-hash"].messages) == 2
    assert not runtime.app.connections and runtime.app.admitted == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["idle", "recording", "inference"])
async def test_periodic_validation_continues_and_detects_revocation(runtime, phase):
    runtime.pipeline.wait = True

    async def incoming():
        if phase == "inference":
            for _ in range(4):
                yield frame()
            yield control("finish")
            await runtime.pipeline.started.wait()
        initial_reads = runtime.db.calls
        runtime.clock.advance(1)
        yield frame() if phase == "recording" else TimeoutError()
        assert runtime.db.calls == initial_reads + 1
        runtime.db.revoked = True
        runtime.clock.advance(1)
        yield frame() if phase == "recording" else TimeoutError()
        pytest.fail("Revoked socket must close before another receive")

    socket = Socket(incoming())
    await asyncio.wait_for(runtime.app.handle(socket), 2)
    assert socket.close_code == 4401
    assert runtime.db.calls == (4 if phase == "inference" else 3)
    assert runtime.pipeline.calls == (1 if phase == "inference" else 0)
    if phase == "inference":
        assert runtime.pipeline.closed.is_set()
    assert not runtime.app.connections and runtime.app.admitted == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["finish", "played"])
async def test_revocation_during_receive_cannot_start_inference_or_commit_history(runtime, kind):
    async def incoming():
        for _ in range(4):
            yield frame()
        if kind == "played":
            yield control("finish")
            await socket.done.wait()
        # The periodic check is not due, but both control boundaries must query again.
        runtime.db.revoked = True
        yield control(kind)

    socket = Socket(incoming())
    await asyncio.wait_for(runtime.app.handle(socket), 2)
    assert socket.close_code == 4401
    assert runtime.pipeline.calls == (1 if kind == "played" else 0)
    assert runtime.db.calls == (3 if kind == "played" else 2)
    assert not runtime.app.histories["session-hash"].messages
    assert not runtime.app.connections and runtime.app.admitted == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["audio", "finish", "played"])
async def test_cached_expiry_is_checked_after_receive_without_waiting_for_database_timer(runtime, kind):
    runtime.db.session_data["expires_at"] = runtime.clock.time() + 0.25

    async def incoming():
        for _ in range(4):
            yield frame()
        if kind == "played":
            yield control("finish")
            await socket.done.wait()
        runtime.clock.advance(0.25)
        yield frame() if kind == "audio" else control(kind)

    socket = Socket(incoming())
    await asyncio.wait_for(runtime.app.handle(socket), 2)
    assert socket.close_code == 4401
    assert runtime.pipeline.calls == (1 if kind == "played" else 0)
    assert runtime.db.calls == (2 if kind == "played" else 1)
    assert not runtime.app.histories["session-hash"].messages
    assert not runtime.app.connections and runtime.app.admitted == 0
