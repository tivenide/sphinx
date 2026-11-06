import asyncio
import base64
import threading

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import Settings
from app.main import create_app
from app.security import COOKIE_NAME, PASSWORD_HASH

ORIGIN = "https://localhost:6500"
PASSWORD = "test-password-12345"


class Pipeline:
    def __init__(self, wait=False):
        self.calls = []
        self.wait = wait
        self.started = threading.Event()
        self.closed = threading.Event()

    async def run(self, pcm, history):
        self.calls.append((pcm, history))
        try:
            self.started.set()
            yield {"type": "state", "state": "thinking"}
            if self.wait:
                await asyncio.sleep(60)
            yield {"type": "audio", "sample_rate": 24000, "data": base64.b64encode(b"\x00\x00" * 240).decode()}
            yield {"type": "result", "transcript": "Eine private Frage", "reply": "Eine private Antwort"}
        finally:
            self.closed.set()


@pytest.fixture
def application(tmp_path):
    secret = tmp_path / "password"
    secret.write_text(PASSWORD)
    pipeline = Pipeline()
    app = create_app(Settings(database=str(tmp_path / "users.sqlite3"), admin_password_file=str(secret)), pipeline=pipeline)
    with TestClient(app, base_url=ORIGIN, headers={"origin": ORIGIN}) as client:
        yield app, client, pipeline


def login(client, username="admin", password=PASSWORD):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return client.cookies.get(COOKIE_NAME)


def test_model_loading_requires_admin_origin_and_empty_request(application):
    app, client, _ = application
    calls = []

    class Models:
        async def request(self, *, load=False):
            calls.append(load)
            return (202 if load else 200), {
                "state": "loading", "can_load": False, "detail": None, "models": [],
            }

    app.state.models = Models()
    assert client.get("/api/models").status_code == 401
    assert client.post("/api/models/load").status_code == 401
    login(client)
    assert client.post("/api/models/load", headers={"origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/models/load", json={"container": "another-app"}).status_code == 400
    assert client.post("/api/models/load?container=another-app").status_code == 400
    assert calls == []
    assert client.get("/api/models").status_code == 200
    assert client.post("/api/models/load").status_code == 202
    assert calls == [False, True]
    assert client.post("/api/users", json={"username": "voice-user", "password": PASSWORD}).status_code == 201
    client.post("/api/auth/logout")
    login(client, "voice-user")
    assert client.get("/api/models").status_code == 403
    assert client.post("/api/models/load").status_code == 403
    assert calls == [False, True]


def test_missing_model_controller_keeps_login_and_voice_health_available(application):
    _, client, _ = application
    login(client)
    response = client.get("/api/models")
    assert response.status_code == 200
    assert response.json()["state"] == "unavailable"
    assert response.json()["can_load"] is False
    assert client.post("/api/models/load").status_code == 409
    assert client.get("/api/auth/me").status_code == 200
    assert client.get("/api/health/live").status_code == 200


def finish_turn(ws, *, played=True):
    assert ws.receive_json()["type"] == "ready"
    for _ in range(4):
        ws.send_bytes(b"\x01\x00" * 512)
    ws.send_json({"type": "finish"})
    events = []
    while True:
        event = ws.receive_json()
        events.append(event)
        if event["type"] == "done":
            break
    assert any(event["type"] == "audio" for event in events)
    assert all(event["type"] not in {"result", "transcript", "reply"} for event in events)
    if played:
        ws.send_json({"type": "played"})
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 1000
    return events


def test_login_cookie_and_logout(application):
    app, client, _ = application
    assert client.get("/api/auth/me").status_code == 401
    response = client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=strict" in cookie
    token = client.cookies[COOKIE_NAME]
    with app.state.db.connection() as db:
        assert db.execute("SELECT token_hash FROM sessions").fetchone()[0] != token
        assert db.execute("SELECT password_hash FROM users").fetchone()[0].startswith("$argon2id$")
    assert client.get("/api/auth/me").json()["role"] == "admin"
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401
    assert app.state.db.session(token) is None


def test_origin_and_chunked_body_limit(application):
    _, client, _ = application
    assert client.post("/api/auth/login", headers={"origin": "https://evil.example"},
                       json={"username": "admin", "password": PASSWORD}).status_code == 403
    response = client.post("/api/auth/login", content=iter([b"a" * 4000, b"b" * 5000]),
                           headers={"content-type": "application/json", "transfer-encoding": "chunked"})
    assert response.status_code == 413
    assert client.post("/api/auth/login", content=b"x" * 9000).status_code == 413


@pytest.mark.parametrize("origin", [ORIGIN, "https://192.168.1.10:6500"])
def test_multiple_origins_support_login_voice_and_logout(tmp_path, origin):
    secret = tmp_path / "password"
    secret.write_text(PASSWORD)
    settings = Settings(
        database=str(tmp_path / "users.sqlite3"), admin_password_file=str(secret),
        additional_public_origins=("https://192.168.1.10:6500",),
    )
    app = create_app(settings, pipeline=Pipeline())
    with TestClient(app, base_url=origin, headers={"origin": origin}) as client:
        login(client)
        assert client.get("/api/auth/me").status_code == 200
        with client.websocket_connect(origin.replace("https://", "wss://") + "/api/voice") as ws:
            finish_turn(ws)
        for denied_origin in ["https://evil.example", "http://192.168.1.10:6500",
                              "https://192.168.1.10:6501", "https://192.168.1.10.evil.example:6500", "null", ""]:
            assert client.post("/api/auth/logout", headers={"origin": denied_origin}).status_code == 403
            with pytest.raises(WebSocketDisconnect) as denied:
                with client.websocket_connect("/api/voice", headers={"origin": denied_origin}):
                    pass
            assert denied.value.code == 4403
        assert client.post("/api/auth/logout").status_code == 204
        assert client.get("/api/auth/me").status_code == 401


def test_roles_last_admin_and_password_revocation(application):
    app, client, _ = application
    admin_token = login(client)
    admin = client.get("/api/auth/me").json()
    assert client.patch(f"/api/users/{admin['id']}", json={"active": False}).status_code == 409
    response = client.post("/api/users", json={"username": "alex", "password": PASSWORD, "role": "user"})
    assert response.status_code == 201
    user_id = response.json()["id"]
    assert client.post("/api/users", json={"username": "Alex", "password": PASSWORD}).status_code == 409
    client.cookies.clear()
    user_token = login(client, "alex")
    assert client.get("/api/users").status_code == 403
    assert client.post("/api/users", json={"username": "evil", "password": PASSWORD}).status_code == 403
    response = client.put(f"/api/users/{user_id}/password", json={"password": PASSWORD + "-new"},
                          headers={"cookie": f"{COOKIE_NAME}={admin_token}"})
    assert response.status_code == 204
    assert app.state.db.session(user_token) is None
    assert client.get("/api/auth/me").status_code == 401


def test_password_reset_during_login_cannot_restore_old_session(application, monkeypatch):
    import app.main as main
    app, client, _ = application
    real_verify = main.verify

    def verify_and_reset(password, encoded):
        correct = real_verify(password, encoded)
        app.state.db.set_password(1, PASSWORD_HASH.hash("replacement-password"))
        return correct

    monkeypatch.setattr(main, "verify", verify_and_reset)
    response = client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    assert response.status_code == 401
    with app.state.db.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_login_rate_limit(application):
    _, client, _ = application
    for _ in range(10):
        assert client.post("/api/auth/login", json={"username": "nobody", "password": "bad"}).status_code == 401
    assert client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}).status_code == 429


def test_websocket_requires_session_and_same_origin(application):
    _, client, _ = application
    with pytest.raises(WebSocketDisconnect) as denied:
        with client.websocket_connect("wss://localhost:6500/api/voice"):
            pass
    assert denied.value.code == 4401
    login(client)
    with pytest.raises(WebSocketDisconnect) as denied:
        with client.websocket_connect("wss://localhost:6500/api/voice", headers={"origin": "https://evil.example"}):
            pass
    assert denied.value.code == 4403


def test_voice_transport_and_history_only_after_playback(application):
    app, client, pipeline = application
    login(client)
    with client.websocket_connect("wss://localhost:6500/api/voice") as ws:
        events = finish_turn(ws)
    assert "private" not in str(events)
    with client.websocket_connect("wss://localhost:6500/api/voice") as ws:
        finish_turn(ws, played=False)
        ws.send_json({"type": "cancel"})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    with client.websocket_connect("wss://localhost:6500/api/voice") as ws:
        finish_turn(ws)
    assert len(pipeline.calls[0][0]) == 4096
    assert pipeline.calls[0][1] == []
    assert len(pipeline.calls[1][1]) == 2
    assert len(pipeline.calls[2][1]) == 2  # Cancelled second response was not committed.
    assert app.state.voice.admitted == 0


def test_context_isolated_between_login_sessions(application):
    _, client, pipeline = application
    login(client)
    with client.websocket_connect("wss://localhost:6500/api/voice") as ws:
        finish_turn(ws)
    client.post("/api/auth/logout")
    login(client)
    with client.websocket_connect("wss://localhost:6500/api/voice") as ws:
        finish_turn(ws)
    assert pipeline.calls[-1][1] == []


def test_cancel_closes_inference_and_releases_user(application):
    app, client, _ = application
    login(client)
    pipeline = Pipeline(wait=True)
    app.state.voice.pipeline = pipeline
    with client.websocket_connect("wss://localhost:6500/api/voice") as ws:
        assert ws.receive_json()["type"] == "ready"
        for _ in range(4):
            ws.send_bytes(b"\x00\x00" * 512)
        ws.send_json({"type": "finish"})
        ws.receive_json()
        ws.receive_json()
        ws.send_json({"type": "cancel"})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert pipeline.closed.wait(2)
    assert app.state.voice.admitted == 0
    with client.websocket_connect("wss://localhost:6500/api/voice") as ws:
        assert ws.receive_json()["type"] == "ready"


def test_one_input_per_user_and_frame_bounds(application):
    _, client, _ = application
    login(client)
    with client.websocket_connect("wss://localhost:6500/api/voice") as first:
        assert first.receive_json()["type"] == "ready"
        with client.websocket_connect("wss://localhost:6500/api/voice") as second:
            assert second.receive_json()["type"] == "error"
        first.send_bytes(b"x" * 1026)
        assert first.receive_json()["type"] == "error"
        with pytest.raises(WebSocketDisconnect) as closed:
            first.receive_json()
        assert closed.value.code == 1008


def test_logout_closes_active_microphone(application):
    app, client, _ = application
    login(client)
    with client.websocket_connect("wss://localhost:6500/api/voice") as ws:
        ws.receive_json()
        assert client.post("/api/auth/logout").status_code == 204
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_json()
        assert closed.value.code == 4401
    assert not app.state.voice.histories


def test_accounts_persist_and_password_bootstrap_is_not_reapplied(application):
    app, client, _ = application
    login(client)
    assert client.put("/api/auth/password", json={"current_password": PASSWORD, "new_password": PASSWORD + "-changed"}).status_code == 204
    fresh = create_app(app.state.settings, pipeline=Pipeline())
    with TestClient(fresh, base_url=ORIGIN, headers={"origin": ORIGIN}) as another:
        assert another.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}).status_code == 401
        login(another, password=PASSWORD + "-changed")
