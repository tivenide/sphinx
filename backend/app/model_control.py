"""Narrow Docker controller: inspect/start only three pre-created Sphinx services."""

from __future__ import annotations

from dataclasses import dataclass
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import socket
import threading
from urllib.parse import quote


@dataclass(frozen=True)
class Model:
    id: str
    label: str
    container: str
    image: str


def configured_models() -> tuple[Model, ...]:
    defaults = (
        ("asr", "Spracherkennung", "sphinx-asr:0.3.0"),
        ("llm", "Antwortmodell", "sphinx-llm:0.3.0"),
        ("tts", "Sprachausgabe", "sphinx-tts:0.3.0"),
    )
    models = []
    for key, label, image in defaults:
        container = os.getenv(f"MODEL_CONTAINER_{key.upper()}", f"sphinx-{key}-1")
        image = os.getenv(f"MODEL_IMAGE_{key.upper()}", image)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", container):
            raise ValueError("Invalid configured model container name")
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@-]{0,255}", image)
                or ".." in image.split("/")):
            raise ValueError("Invalid configured model image name")
        models.append(Model(key, label, container, image))
    return tuple(models)


class DockerError(Exception):
    def __init__(self, status: int = 0, detail: str = "Der Docker-Dienst ist nicht erreichbar."):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def safe_docker_error(message: str) -> str:
    lowered = message.lower()
    if "mismatch" in lowered and ("nvml" in lowered or "driver/library" in lowered):
        return "Der NVIDIA-Treiber meldet einen Versionskonflikt. Der Modellstart ist blockiert."
    if any(word in lowered for word in ("nvidia", "cuda", "gpu")):
        return "Die GPU ist für den Modellstart nicht verfügbar."
    return "Der Modelldienst konnte nicht gestartet werden. Bitte die Serverkonfiguration prüfen."


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class Docker:
    """No Docker CLI, arbitrary URLs, container creation, image pulls or exec."""

    def __init__(self, models: tuple[Model, ...], socket_path="/var/run/docker.sock", exchange=None):
        if not socket_path.startswith("/") or socket_path.startswith("//"):
            raise ValueError("Docker must use an absolute Unix socket path")
        self.socket_path = socket_path
        self.allowed = set()
        for model in models:
            self.allowed.update({
                ("GET", self.container_path(model) + "/json"),
                ("POST", self.container_path(model) + "/start"),
                ("GET", "/images/" + quote(model.image, safe="") + "/json"),
            })
        self.exchange = exchange or self._exchange

    @staticmethod
    def container_path(model: Model):
        return "/containers/" + quote(model.container, safe="")

    def _exchange(self, method: str, path: str):
        connection = UnixConnection(self.socket_path, timeout=20 if method == "POST" else 3)
        try:
            connection.request(method, path, headers={"Content-Length": "0"})
            response = connection.getresponse()
            body = response.read(262145)
            if len(body) > 262144:
                raise DockerError()
            return response.status, body
        except (OSError, http.client.HTTPException) as error:
            raise DockerError() from error
        finally:
            connection.close()

    def request(self, method: str, path: str):
        if (method, path) not in self.allowed:
            raise ValueError("Docker operation is not allowed")
        status, body = self.exchange(method, path)
        if status >= 400:
            raise DockerError(status, safe_docker_error(body.decode("utf-8", errors="replace")))
        if not body:
            return status, {}
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError()
        except (ValueError, UnicodeError) as error:
            raise DockerError() from error
        return status, payload

    def inspect(self, model: Model):
        status, payload = self.request("GET", self.container_path(model) + "/json")
        if status != 200:
            raise DockerError()
        return payload

    def image_exists(self, model: Model):
        try:
            status, _ = self.request("GET", "/images/" + quote(model.image, safe="") + "/json")
            if status != 200:
                raise DockerError()
            return True
        except DockerError as error:
            if error.status == 404:
                return False
            raise

    def start(self, model: Model):
        status, _ = self.request("POST", self.container_path(model) + "/start")
        if status not in (204, 304):
            raise DockerError()


class Controller:
    def __init__(self, docker: Docker, models: tuple[Model, ...], project="sphinx"):
        self.docker = docker
        self.models = models
        self.project = project
        self.lock = threading.RLock()
        self.starting = False
        self.errors: dict[str, str] = {}
        self.worker: threading.Thread | None = None

    def _inspect(self, model: Model):
        row = {"id": model.id, "label": model.label, "state": "unavailable", "detail": None}
        try:
            container = self.docker.inspect(model)
        except DockerError as error:
            if error.status != 404:
                return {**row, "detail": "Der Docker-Dienst ist nicht erreichbar."}, False
            try:
                exists = self.docker.image_exists(model)
            except DockerError:
                return {**row, "detail": "Der Docker-Dienst ist nicht erreichbar."}, False
            if not exists:
                label = "vLLM-Image" if model.id == "llm" else "Modell-Image"
                return {**row, "detail": f"Das benötigte {label} fehlt. Es wird nichts heruntergeladen."}, False
            return {**row, "detail": "Der Modelldienst ist noch nicht vorbereitet."}, False

        config = container.get("Config") or {}
        labels = config.get("Labels") or {}
        if (labels.get("com.docker.compose.project") != self.project
                or labels.get("com.docker.compose.service") != model.id
                or labels.get("com.docker.compose.oneoff", "False").lower() != "false"
                or config.get("Image") != model.image):
            return {**row, "detail": "Der Modelldienst gehört nicht zur freigegebenen Sphinx-Konfiguration."}, False
        env = config.get("Env") or []
        offline = all([item for item in env if item.startswith(key + "=")] == [key + "=1"]
                      for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE"))
        if not offline:
            return {**row, "detail": "Der Modelldienst ist nicht für den Offlinebetrieb vorbereitet."}, False

        state = container.get("State") or {}
        if state.get("Paused"):
            return {**row, "detail": "Der Modelldienst ist pausiert und muss serverseitig fortgesetzt werden."}, False
        if state.get("Running"):
            health = (state.get("Health") or {}).get("Status")
            if health == "healthy":
                return {**row, "state": "ready"}, False
            if health == "unhealthy":
                return {**row, "state": "error", "detail": "Der Modelldienst ist gestartet, aber nicht einsatzbereit."}, False
            return {**row, "state": "loading", "detail": "Das Modell wird geladen."}, False
        if state.get("Error") or state.get("ExitCode", 0):
            detail = safe_docker_error(str(state.get("Error", "")))
            return {**row, "state": "error", "detail": detail}, True
        return {**row, "state": "not_loaded", "detail": "Das Modell ist noch nicht geladen."}, True

    def _inspect_all(self):
        return [self._inspect(model) for model in self.models]

    def _snapshot(self, inspected):
        rows = []
        for original, startable in inspected:
            row = dict(original)
            if self.starting and startable:
                row.update(state="loading", detail="Der Modelldienst wird gestartet.")
            elif row["id"] in self.errors and row["state"] != "ready":
                row.update(state="error", detail=self.errors[row["id"]])
            rows.append(row)
        states = [row["state"] for row in rows]
        if self.starting:
            state, detail = "loading", "Die Modelle werden gestartet."
        elif all(value == "ready" for value in states):
            state, detail = "ready", None
        elif "unavailable" in states:
            unavailable = [row for row in rows if row["state"] == "unavailable"]
            unavailable.sort(key=lambda row: row["id"] != "llm")
            state, detail = "unavailable", unavailable[0]["detail"]
        elif "error" in states:
            state, detail = "error", next(row["detail"] for row in rows if row["state"] == "error")
        elif "loading" in states:
            state, detail = "loading", "Die Modelle werden geladen."
        else:
            state, detail = "not_loaded", "Die Modelle sind noch nicht geladen."
        can_load = (not self.starting and "unavailable" not in states and "loading" not in states
                    and any(startable for _, startable in inspected)
                    and all(startable or row["state"] == "ready" for row, startable in inspected))
        return {"state": state, "can_load": can_load, "detail": detail, "models": rows}

    def status(self):
        with self.lock:
            return self._snapshot(self._inspect_all())

    def load(self):
        with self.lock:
            inspected = self._inspect_all()
            snapshot = self._snapshot(inspected)
            if self.starting or snapshot["state"] in ("ready", "loading"):
                return 202, snapshot
            if not snapshot["can_load"]:
                return 409, snapshot
            self.errors.clear()
            self.starting = True
            todo = [model for model, (_, startable) in zip(self.models, inspected) if startable]
            # The largest model starts first, so a failed LLM start cannot start ASR/TTS.
            todo.sort(key=lambda model: model.id != "llm")
            self.worker = threading.Thread(target=self._start, args=(todo,), daemon=True)
            self.worker.start()
            return 202, self._snapshot(inspected)

    def _start(self, models):
        try:
            for model in models:
                row, startable = self._inspect(model)
                if row["state"] == "ready" or row["state"] == "loading":
                    continue
                if not startable:
                    raise DockerError(detail=row["detail"] or "Der Modellstart ist blockiert.")
                try:
                    self.docker.start(model)
                except DockerError as error:
                    with self.lock:
                        self.errors[model.id] = error.detail
                    return
        except DockerError as error:
            with self.lock:
                self.errors[model.id] = error.detail
        except Exception:
            with self.lock:
                self.errors[model.id] = "Der Modellstart wurde unterbrochen."
        finally:
            with self.lock:
                self.starting = False


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, controller: Controller, token: str):
        if len(token) < 32:
            raise ValueError("Model-control token must contain at least 32 characters")
        self.controller, self.token = controller, token
        super().__init__(address, Handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address


class Handler(BaseHTTPRequestHandler):
    server: Server

    def log_message(self, format, *args):
        pass

    def reply(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def dispatch(self):
        auth = self.headers.get_all("Authorization", [])
        valid = (len(auth) == 1 and auth[0].isascii()
                 and hmac.compare_digest(auth[0], "Bearer " + self.server.token))
        if not valid:
            self.reply(401, {"detail": "Nicht autorisiert."})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if (self.headers.get("Transfer-Encoding") is not None or len(lengths) > 1
                or (lengths and lengths != ["0"])):
            self.reply(400, {"detail": "Anfragedaten sind nicht erlaubt."})
            return
        if self.path not in ("/status", "/load"):
            self.reply(404, {"detail": "Unbekannte Aktion."})
            return
        if (self.command, self.path) not in (("GET", "/status"), ("POST", "/load")):
            self.reply(405, {"detail": "Methode nicht erlaubt."})
            return
        try:
            if self.command == "GET":
                self.reply(200, self.server.controller.status())
            else:
                self.reply(*self.server.controller.load())
        except Exception:
            self.reply(503, {"state": "unavailable", "can_load": False,
                             "detail": "Die Modellsteuerung ist nicht erreichbar.",
                             "models": [{"id": model.id, "label": model.label,
                                         "state": "unavailable", "detail": "Status nicht verfügbar."}
                                        for model in self.server.controller.models]})

    do_GET = dispatch
    do_POST = dispatch
    do_PUT = dispatch
    do_PATCH = dispatch
    do_DELETE = dispatch
    do_OPTIONS = dispatch
    do_HEAD = dispatch


def main():
    token = Path(os.environ["MODEL_CONTROL_TOKEN_FILE"]).read_text().strip()
    models = configured_models()
    controller = Controller(Docker(models), models, os.getenv("MODEL_PROJECT", "sphinx"))
    server = Server(("0.0.0.0", int(os.getenv("MODEL_CONTROL_PORT", "8082"))), controller, token)
    server.serve_forever(poll_interval=0.5)


if __name__ == "__main__":
    main()
