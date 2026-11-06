import asyncio
import sqlite3
from contextlib import asynccontextmanager
from typing import Literal

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from .config import Settings
from .boundaries import HttpBoundary
from .database import Database, public_user, token_digest
from .documents import document_overview
from .ingestion import IngestionClient
from .knowledge import KnowledgeRetriever
from .model_loading import ModelControlClient
from .security import COOKIE_NAME, DUMMY_HASH, PASSWORD_HASH, LoginLimiter, valid_password, verify
from .speech import SpeechPipeline
from .voice import VoiceRuntime


class Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Login(Payload):
    username: str = Field(min_length=1, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class NewUser(Payload):
    username: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._-]{2,31}$")
    password: str = Field(min_length=12, max_length=128)
    role: Literal["admin", "user"] = "user"


class UpdateUser(Payload):
    active: bool | None = None
    role: Literal["admin", "user"] | None = None


class PasswordChange(Payload):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


class PasswordReset(Payload):
    password: str = Field(min_length=12, max_length=128)


async def current_user(request: Request):
    user = await run_in_threadpool(request.app.state.db.session, request.cookies.get(COOKIE_NAME, ""))
    if user is None:
        raise HTTPException(401, "Bitte anmelden.")
    return user


async def admin_user(user=Depends(current_user)):
    if user["role"] != "admin":
        raise HTTPException(403, "Nur für Administratoren.")
    return user


def create_app(settings: Settings | None = None, pipeline=None):
    settings = settings or Settings.from_env()
    db = Database(settings.database)
    limiter = LoginLimiter()

    @asynccontextmanager
    async def lifespan(app):
        await run_in_threadpool(db.initialize)
        if not await run_in_threadpool(db.has_users):
            password = settings.initial_password()
            if password:
                valid_password(password)
                initial = NewUser(username=settings.admin_username, password=password, role="admin")
                encoded = await run_in_threadpool(PASSWORD_HASH.hash, initial.password)
                await run_in_threadpool(db.create_user, initial.username.lower(), encoded, "admin")
        async with httpx.AsyncClient(timeout=httpx.Timeout(settings.request_timeout, connect=5),
                                     limits=httpx.Limits(max_connections=12, max_keepalive_connections=6)) as client:
            app.state.http = client
            app.state.models = ModelControlClient(client, settings)
            app.state.ingestion = await run_in_threadpool(IngestionClient, client, settings, db)
            knowledge = KnowledgeRetriever(client, settings)
            app.state.knowledge = knowledge
            await knowledge.start()
            app.state.voice = VoiceRuntime(db, settings, pipeline or SpeechPipeline(client, settings, knowledge))
            try:
                yield
            finally:
                await app.state.voice.shutdown()
                await knowledge.close()

    app = FastAPI(title="Sphinx", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db = db
    app.state.settings = settings

    app.add_middleware(HttpBoundary, origins=settings.allowed_origins)

    @app.get("/api/health/live")
    async def live():
        return {"status": "ok"}

    @app.get("/api/health/ready")
    async def ready(request: Request):
        async def probe(url, *, tts=False):
            try:
                response = await request.app.state.http.get(url, timeout=2)
                return response.is_success and (not tts or response.json().get("status") == "ready")
            except (httpx.HTTPError, ValueError, AttributeError):
                return False
        base = lambda url: url.split("/v1/")[0]
        asr, llm, tts = await asyncio.gather(
            probe(base(settings.asr_url) + "/ready"),
            probe(base(settings.llm_url) + "/health"),
            probe(base(settings.tts_url) + "/status", tts=True),
        )
        return JSONResponse({"ready": bool(asr and llm and tts), "services": {"asr": asr, "llm": llm, "tts": tts}},
                            status_code=200 if asr and llm and tts else 503)

    @app.post("/api/auth/login")
    async def login(body: Login, request: Request, response: Response):
        ip = request.client.host if request.client else "unknown"
        if not limiter.allow(ip):
            raise HTTPException(429, "Zu viele Anmeldeversuche. Bitte eine Minute warten.")
        user = await run_in_threadpool(db.find_user, body.username.lower())
        correct = await run_in_threadpool(verify, body.password, user["password_hash"] if user else DUMMY_HASH)
        if not correct or not user or not user["active"]:
            raise HTTPException(401, "Benutzername oder Passwort ist falsch.")
        previous = request.cookies.get(COOKIE_NAME, "")
        if previous:
            await run_in_threadpool(db.revoke_session, previous)
            await app.state.voice.revoke(token_hash=token_digest(previous))
        try:
            token = await run_in_threadpool(db.new_session, user["id"], settings.session_seconds, user["password_hash"])
        except ValueError:
            raise HTTPException(401, "Benutzername oder Passwort ist falsch.") from None
        response.set_cookie(COOKIE_NAME, token, max_age=settings.session_seconds, secure=settings.cookie_secure,
                            httponly=True, samesite="strict", path="/api")
        return public_user(user)

    @app.get("/api/models")
    async def model_status(user=Depends(admin_user)):
        code, snapshot = await app.state.models.request()
        return JSONResponse(snapshot, status_code=code)

    @app.post("/api/models/load")
    async def load_models(request: Request, user=Depends(admin_user)):
        if request.query_params or await request.body():
            raise HTTPException(400, "Zum Laden werden keine Parameter benötigt.")
        code, snapshot = await app.state.models.request(load=True)
        return JSONResponse(snapshot, status_code=code)

    @app.get("/api/auth/me")
    async def me(user=Depends(current_user)):
        return public_user(user)

    @app.post("/api/ingestion/documents")
    async def ingest_document(request: Request, user=Depends(admin_user)):
        return await app.state.ingestion.upload(request)

    @app.get("/api/ingestion/documents")
    async def documents(user=Depends(admin_user), limit: int = Query(25, ge=1, le=100),
                        documents_offset: int = Query(0, ge=0, le=1_000_000),
                        attempts_offset: int = Query(0, ge=0, le=1_000_000)):
        return await document_overview(db, app.state.knowledge.pool, limit=limit,
                                       documents_offset=documents_offset, attempts_offset=attempts_offset)

    @app.post("/api/auth/logout", status_code=204)
    async def logout(request: Request):
        token = request.cookies.get(COOKIE_NAME, "")
        await run_in_threadpool(db.revoke_session, token)
        await app.state.voice.revoke(token_hash=token_digest(token))
        response = Response(status_code=204)
        response.delete_cookie(COOKIE_NAME, path="/api", secure=settings.cookie_secure, httponly=True, samesite="strict")
        return response

    @app.put("/api/auth/password", status_code=204)
    async def password(body: PasswordChange, user=Depends(current_user)):
        if not await run_in_threadpool(verify, body.current_password, user["password_hash"]):
            raise HTTPException(400, "Das aktuelle Passwort ist falsch.")
        encoded = await run_in_threadpool(PASSWORD_HASH.hash, body.new_password)
        await run_in_threadpool(db.set_password, user["id"], encoded)
        await app.state.voice.revoke(user_id=user["id"])
        response = Response(status_code=204)
        response.delete_cookie(COOKIE_NAME, path="/api", secure=settings.cookie_secure, httponly=True, samesite="strict")
        return response

    @app.get("/api/users")
    async def users(user=Depends(admin_user)):
        return await run_in_threadpool(db.list_users)

    @app.post("/api/users", status_code=201)
    async def add_user(body: NewUser, user=Depends(admin_user)):
        encoded = await run_in_threadpool(PASSWORD_HASH.hash, body.password)
        try:
            return await run_in_threadpool(db.create_user, body.username.lower(), encoded, body.role)
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Dieser Benutzername ist bereits vergeben.") from None

    @app.patch("/api/users/{user_id}")
    async def update_user(user_id: int, body: UpdateUser, user=Depends(admin_user)):
        if body.active is None and body.role is None:
            raise HTTPException(400, "Keine Änderung angegeben.")
        try:
            updated = await run_in_threadpool(db.update_user, user_id, active=body.active, role=body.role)
        except LookupError:
            raise HTTPException(404, "Benutzer nicht gefunden.") from None
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        await app.state.voice.revoke(user_id=user_id)
        return updated

    @app.put("/api/users/{user_id}/password", status_code=204)
    async def reset_password(user_id: int, body: PasswordReset, user=Depends(admin_user)):
        encoded = await run_in_threadpool(PASSWORD_HASH.hash, body.password)
        try:
            await run_in_threadpool(db.set_password, user_id, encoded)
        except LookupError:
            raise HTTPException(404, "Benutzer nicht gefunden.") from None
        await app.state.voice.revoke(user_id=user_id)
        return Response(status_code=204)

    @app.websocket("/api/voice")
    async def voice(ws: WebSocket):
        await app.state.voice.handle(ws)

    return app


app = create_app()
