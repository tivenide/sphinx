"""Control-plane tests: no real Docker operations or GPU/model starts."""

from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import sys
import threading
from urllib.parse import unquote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.model_control import Controller, Docker, DockerError, Model, Server, configured_models


MODELS = tuple(Model(key, label, f"sphinx-{key}-1", image) for key, label, image in (
    ("asr", "Spracherkennung", "sphinx-asr:0.3.0"),
    ("llm", "Antwortmodell", "sphinx-llm:0.3.0"),
    ("tts", "Sprachausgabe", "sphinx-tts:0.3.0"),
))
TOKEN = "test-only-controller-token-32-characters"


class FakeDocker:
    def __init__(self):
        self.containers = {
            model.container: {
                "Config": {
                    "Image": model.image,
                    "Labels": {"com.docker.compose.project": "sphinx",
                               "com.docker.compose.service": model.id,
                               "com.docker.compose.oneoff": "False"},
                    "Env": ["HF_HUB_OFFLINE=1", "TRANSFORMERS_OFFLINE=1"],
                },
                "State": {"Running": False, "Status": "created", "ExitCode": 0},
            } for model in MODELS
        }
        self.images = {model.image for model in MODELS}
        self.calls = []
        self.failure = None
        self.started = threading.Event()
        self.release = None

    def __call__(self, method, path):
        self.calls.append((method, path))
        if path.startswith("/images/"):
            image = unquote(path[len("/images/"):-len("/json")])
            return (200, b'{"Id":"sha256:test"}') if image in self.images else (404, b'{}')
        name = unquote(path.split("/")[2])
        if name not in self.containers:
            return 404, b'{}'
        if method == "GET":
            return 200, json.dumps(self.containers[name]).encode()
        self.started.set()
        if self.release is not None:
            assert self.release.wait(3), "test release timed out"
        if self.failure:
            return 500, json.dumps({"message": self.failure}).encode()
        self.containers[name]["State"] = {"Running": True, "Health": {"Status": "starting"}}
        return 204, b''

    @property
    def starts(self):
        return [path for method, path in self.calls if method == "POST"]


@pytest.fixture
def control():
    fake = FakeDocker()
    return Controller(Docker(MODELS, exchange=fake), MODELS), fake


@contextmanager
def http_control(controller):
    server = Server(("127.0.0.1", 0), controller, TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        def request(method="GET", path="/status", *, token=TOKEN, body=None, headers=None):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            request_headers = {"Authorization": "Bearer " + token} if token else {}
            request_headers.update(headers or {})
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            data = response.read()
            connection.close()
            return response.status, json.loads(data) if data else None
        yield request
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_controller_authentication_precedes_docker_access(control):
    controller, fake = control
    with http_control(controller) as request:
        assert request(token="")[0] == 401
        assert request("POST", "/load", token="incorrect")[0] == 401
        assert request(token="ungültig")[0] == 401
        assert fake.calls == []
        status, snapshot = request()
        assert status == 200
        assert snapshot["state"] == "not_loaded"
        assert snapshot["can_load"] is True
        assert [row["id"] for row in snapshot["models"]] == ["asr", "llm", "tts"]
        assert fake.starts == []


@pytest.mark.parametrize("method,path,body,headers,expected", [
    ("GET", "/status?container=foreign", None, {}, 404),
    ("POST", "/load?image=anything", None, {}, 404),
    ("POST", "/containers/foreign/start", None, {}, 404),
    ("PUT", "/load", None, {}, 405),
    ("DELETE", "/status", None, {}, 405),
    ("POST", "/load", b"{}", {}, 400),
    ("GET", "/status", b"payload", {}, 400),
    ("POST", "/load", b"0\r\n\r\n", {"Transfer-Encoding": "chunked"}, 400),
])
def test_controller_rejects_query_payload_and_other_operations(control, method, path, body, headers, expected):
    controller, fake = control
    with http_control(controller) as request:
        assert request(method, path, body=body, headers=headers)[0] == expected
    assert fake.calls == []


@pytest.mark.parametrize("method,path", [
    ("POST", "/containers/sphinx-asr-1/stop"),
    ("POST", "/containers/foreign/start"),
    ("POST", "/containers/create"),
    ("POST", "/images/create"),
    ("GET", "/containers/json"),
    ("GET", "/containers/sphinx-asr-1/json?size=1"),
    ("GET", "/images/other/json"),
])
def test_docker_transport_has_exact_allowlist(method, path):
    fake = FakeDocker()
    docker = Docker(MODELS, exchange=fake)
    with pytest.raises(ValueError, match="not allowed"):
        docker.request(method, path)
    assert fake.calls == []
    with pytest.raises(ValueError, match="Unix"):
        Docker(MODELS, socket_path="tcp://localhost:2375")


def test_missing_image_or_container_blocks_every_start(control):
    controller, fake = control
    del fake.containers[MODELS[1].container]
    fake.images.remove(MODELS[1].image)
    status, snapshot = controller.load()
    assert status == 409
    assert snapshot["state"] == "unavailable"
    assert snapshot["can_load"] is False
    assert "vLLM-Image fehlt" in snapshot["detail"]
    assert fake.starts == []
    fake.images.add(MODELS[1].image)
    status, snapshot = controller.load()
    assert status == 409
    assert "nicht vorbereitet" in snapshot["models"][1]["detail"]
    assert fake.starts == []


@pytest.mark.parametrize("label,value", [
    ("com.docker.compose.project", "other-project"),
    ("com.docker.compose.service", "frontend"),
    ("com.docker.compose.oneoff", "True"),
])
def test_foreign_or_oneoff_container_labels_block_all_starts(control, label, value):
    controller, fake = control
    fake.containers[MODELS[2].container]["Config"]["Labels"][label] = value
    status, snapshot = controller.load()
    assert status == 409
    assert "freigegebenen Sphinx-Konfiguration" in snapshot["models"][2]["detail"]
    assert fake.starts == []


@pytest.mark.parametrize("env", [
    [], ["HF_HUB_OFFLINE=1"], ["HF_HUB_OFFLINE=0", "TRANSFORMERS_OFFLINE=1"],
    ["HF_HUB_OFFLINE=1", "HF_HUB_OFFLINE=0", "TRANSFORMERS_OFFLINE=1"],
])
def test_offline_mode_is_required_before_any_start(control, env):
    controller, fake = control
    fake.containers[MODELS[2].container]["Config"]["Env"] = env
    status, snapshot = controller.load()
    assert status == 409
    assert "Offlinebetrieb" in snapshot["models"][2]["detail"]
    assert fake.starts == []


def test_wrong_image_cannot_be_started(control):
    controller, fake = control
    fake.containers[MODELS[0].container]["Config"]["Image"] = "unexpected-image:latest"
    assert controller.load()[0] == 409
    assert fake.starts == []


def test_start_is_asynchronous_and_repeated_clicks_are_idempotent(control):
    controller, fake = control
    fake.release = threading.Event()
    first_status, first = controller.load()
    assert first_status == 202
    assert first["state"] == "loading"
    assert first["can_load"] is False
    assert fake.started.wait(1)
    original_worker = controller.worker
    second_status, second = controller.load()
    assert second_status == 202 and second["state"] == "loading"
    assert controller.worker is original_worker
    fake.release.set()
    original_worker.join(timeout=2)
    assert not original_worker.is_alive()
    assert fake.starts == ["/containers/sphinx-llm-1/start", "/containers/sphinx-asr-1/start",
                           "/containers/sphinx-tts-1/start"]
    # Running does not mean loaded: readiness requires the container health check.
    assert controller.status()["state"] == "loading"
    for container in fake.containers.values():
        container["State"]["Health"]["Status"] = "healthy"
    assert controller.status()["state"] == "ready"
    assert controller.load()[0] == 202
    assert len(fake.starts) == 3


def test_failed_start_stops_remaining_starts_and_sanitizes_errors(control):
    controller, fake = control
    fake.failure = "nvidia-container-cli: nvml error: driver/library version mismatch, secret=/private/password"
    assert controller.load()[0] == 202
    controller.worker.join(timeout=2)
    assert fake.starts == ["/containers/sphinx-llm-1/start"]
    snapshot = controller.status()
    assert snapshot["state"] == "error"
    assert "NVIDIA-Treiber" in snapshot["detail"]
    assert "Versionskonflikt" in snapshot["detail"]
    assert "secret" not in json.dumps(snapshot) and "private" not in json.dumps(snapshot)
    assert snapshot["can_load"] is True  # A later retry after host repair is permitted.


def test_already_running_healthy_model_is_not_restarted(control):
    controller, fake = control
    fake.containers[MODELS[1].container]["State"] = {"Running": True, "Health": {"Status": "healthy"}}
    assert controller.load()[0] == 202
    controller.worker.join(timeout=2)
    assert "/containers/sphinx-llm-1/start" not in fake.starts
    assert len(fake.starts) == 2


def test_unhealthy_running_service_is_not_reported_ready_or_restarted(control):
    controller, fake = control
    fake.containers[MODELS[1].container]["State"] = {"Running": True, "Health": {"Status": "unhealthy"}}
    status, snapshot = controller.load()
    assert status == 409 and snapshot["state"] == "error"
    assert snapshot["can_load"] is False
    assert fake.starts == []


def test_docker_failure_reports_safe_unavailability():
    def broken(method, path):
        raise DockerError(detail="untrusted daemon detail")
    controller = Controller(Docker(MODELS, exchange=broken), MODELS)
    status, snapshot = controller.load()
    assert status == 409 and snapshot["state"] == "unavailable"
    assert "untrusted" not in json.dumps(snapshot)


def test_http_blocked_load_returns_a_snapshot_not_an_exception_detail(control):
    controller, fake = control
    del fake.containers[MODELS[1].container]
    with http_control(controller) as request:
        status, snapshot = request("POST", "/load")
    assert status == 409
    assert set(snapshot) == {"state", "can_load", "detail", "models"}
    assert fake.starts == []


def test_configuration_refuses_paths_and_short_tokens(monkeypatch, control):
    controller, _ = control
    monkeypatch.setenv("MODEL_CONTAINER_ASR", "../foreign")
    with pytest.raises(ValueError, match="container"):
        configured_models()
    with pytest.raises(ValueError, match="32"):
        Server(("127.0.0.1", 0), controller, "short")
