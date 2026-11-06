"""One authenticated PCM input per user; no uploads, daemon or parallel input lanes."""

import asyncio
import contextlib
import json
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from fastapi import WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from .security import COOKIE_NAME
from .speech import SpeechError


@dataclass
class Conversation:
    user_id: int
    expires_at: float
    messages: list = field(default_factory=list)


class VoiceRuntime:
    def __init__(self, database, settings, pipeline):
        self.database = database
        self.settings = settings
        self.pipeline = pipeline
        self.connections: dict[int, tuple[str, WebSocket]] = {}
        self.histories: OrderedDict[str, Conversation] = OrderedDict()
        self.inference = asyncio.Semaphore(1)
        self.admitted = 0

    def history(self, session):
        now = time.time()
        for key in list(self.histories):
            if self.histories[key].expires_at <= now:
                del self.histories[key]
        key = session["token_hash"]
        if key not in self.histories:
            self.histories[key] = Conversation(session["id"], session["expires_at"])
        self.histories.move_to_end(key)
        while len(self.histories) > 256:
            self.histories.popitem(last=False)
        return self.histories[key]

    async def revoke(self, *, user_id=None, token_hash=None):
        for key, value in list(self.histories.items()):
            if value.user_id == user_id or key == token_hash:
                del self.histories[key]
        for uid, (key, ws) in list(self.connections.items()):
            if uid == user_id or key == token_hash:
                with contextlib.suppress(Exception):
                    await ws.close(code=4401)

    async def shutdown(self):
        for user_id in list(self.connections):
            await self.revoke(user_id=user_id)
        self.histories.clear()

    async def handle(self, ws: WebSocket):
        if ws.headers.get("origin") not in self.settings.allowed_origins:
            await ws.close(code=4403)
            return
        token = ws.cookies.get(COOKIE_NAME, "")
        session = await run_in_threadpool(self.database.session, token)
        if session is None:
            await ws.close(code=4401)
            return
        await ws.accept()
        if session["id"] in self.connections:
            await ws.send_json({"type": "error", "detail": "In einem anderen Fenster ist bereits eine Aufnahme aktiv."})
            await ws.close(code=4409)
            return

        self.connections[session["id"]] = (session["token_hash"], ws)
        conversation = self.history(session)
        pcm = bytearray()
        task = None
        result = None
        completed = False
        admitted = False
        started = time.monotonic()
        capture_deadline = started + self.settings.max_record_seconds + 30  # Allow the permission prompt.
        last_frame = started
        sent_done_at = None
        session_expires_at = session["expires_at"]
        next_session_check = started + 1

        async def session_valid(*, force=False):
            nonlocal session_expires_at, next_session_check
            if time.time() >= session_expires_at:
                await ws.close(code=4401)
                return False
            if force or time.monotonic() >= next_session_check:
                current = await run_in_threadpool(self.database.session, token)
                next_session_check = time.monotonic() + 1
                if current is None or time.time() >= current["expires_at"]:
                    await ws.close(code=4401)
                    return False
                session_expires_at = current["expires_at"]
            return True

        async def send(event):
            await asyncio.wait_for(ws.send_json(event), timeout=5)

        async def generate(audio):
            nonlocal result, completed, sent_done_at
            try:
                async with asyncio.timeout(self.settings.request_timeout):
                    async with self.inference:
                        async with contextlib.aclosing(self.pipeline.run(audio, list(conversation.messages))) as events:
                            async for event in events:
                                if event["type"] == "result":
                                    result = event
                                else:
                                    await send(event)
                if result is None:
                    raise SpeechError("Die Antwort konnte nicht abgeschlossen werden.")
                completed = True
                sent_done_at = time.monotonic()
                await send({"type": "done"})
            except asyncio.CancelledError:
                raise
            except (SpeechError, TimeoutError) as error:
                detail = str(error) if isinstance(error, SpeechError) else "Die Antwort hat zu lange gedauert. Bitte erneut versuchen."
                await send({"type": "error", "detail": detail})
                await ws.close(code=1011)
            except (WebSocketDisconnect, RuntimeError):
                pass
            except Exception:
                # Never expose transcripts or upstream response bodies at this boundary.
                with contextlib.suppress(Exception):
                    await send({"type": "error", "detail": "Die Sprachausgabe wurde unterbrochen."})
                    await ws.close(code=1011)

        try:
            await send({"type": "ready", "sample_rate": 16000, "frame_samples": 512,
                        "max_seconds": self.settings.max_record_seconds})
            while True:
                if not await session_valid():
                    return
                now = time.monotonic()
                if task is None and (now > capture_deadline or (pcm and now - last_frame > 10)):
                    raise ValueError("Die Aufnahme wurde wegen Zeitüberschreitung beendet.")
                if sent_done_at is not None and now - sent_done_at > 120:
                    raise ValueError("Die Wiedergabe wurde nicht bestätigt.")
                if task is not None and task.done() and not completed:
                    return
                try:
                    # Recheck idle sockets on schedule, without querying SQLite for every audio frame.
                    timeout = max(0.001, min(next_session_check - time.monotonic(), session_expires_at - time.time()))
                    message = await asyncio.wait_for(ws.receive(), timeout=timeout)
                except TimeoutError:
                    continue
                if message["type"] == "websocket.disconnect":
                    return
                if message.get("bytes") is not None:
                    if not await session_valid():
                        return
                    frame = message["bytes"]
                    if task is not None or not frame or len(frame) > 1024 or len(frame) % 2:
                        raise ValueError("Ungültiger Audiostream.")
                    if len(pcm) + len(frame) > self.settings.max_record_seconds * 32000:
                        raise ValueError("Die maximale Aufnahmedauer beträgt 30 Sekunden.")
                    pcm.extend(frame)
                    last_frame = time.monotonic()
                    continue
                text = message.get("text", "")
                if len(text) > 128:
                    raise ValueError("Ungültige Audiosteuerung.")
                try:
                    event = json.loads(text)
                except (json.JSONDecodeError, TypeError):
                    raise ValueError("Ungültige Audiosteuerung.") from None
                if not isinstance(event, dict):
                    raise ValueError("Ungültige Audiosteuerung.")
                # A session may expire or be revoked while receive() is waiting.
                if not await session_valid(force=event.get("type") in ("finish", "played")):
                    return
                if event.get("type") == "cancel":
                    return
                if event.get("type") == "finish" and task is None:
                    if len(pcm) < 3200:
                        raise ValueError("Die Aufnahme ist zu kurz. Bitte erneut sprechen.")
                    if self.admitted >= 5:
                        raise ValueError("Der Sprachdienst ist ausgelastet. Bitte gleich erneut versuchen.")
                    self.admitted += 1
                    admitted = True
                    audio = bytes(pcm)
                    pcm.clear()
                    await send({"type": "state", "state": "thinking"})
                    task = asyncio.create_task(generate(audio))
                    continue
                if event.get("type") == "played" and completed and result:
                    # Only completed playback enters hidden context. Interrupted replies are omitted.
                    conversation.messages.extend([
                        {"role": "user", "content": result["transcript"][:4000]},
                        {"role": "assistant", "content": result["reply"][:4000]},
                    ])
                    conversation.messages[:] = conversation.messages[-12:]
                    await ws.close(code=1000)
                    return
                raise ValueError("Ungültiger Ablauf der Sprachaufnahme.")
        except (WebSocketDisconnect, RuntimeError):
            pass
        except ValueError as error:
            with contextlib.suppress(Exception):
                await send({"type": "error", "detail": str(error)})
                await ws.close(code=1008)
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            if admitted:
                self.admitted -= 1
            pcm.clear()
            if self.connections.get(session["id"], (None, None))[1] is ws:
                self.connections.pop(session["id"], None)
            with contextlib.suppress(Exception):
                await ws.close(code=1000)
