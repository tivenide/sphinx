"""Authenticated client for the private, fixed-scope model launcher."""

from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError


State = Literal["not_loaded", "loading", "ready", "error", "unavailable"]
LABELS = {"asr": "Spracherkennung", "llm": "Antwortmodell", "tts": "Sprachausgabe"}


class ModelStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: Literal["asr", "llm", "tts"]
    label: str
    state: State
    detail: str | None = None


class ModelSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: State
    can_load: bool
    detail: str | None = None
    models: list[ModelStatus]


def unavailable():
    detail = "Die Modellsteuerung ist nicht erreichbar oder noch nicht eingerichtet."
    return {
        "state": "unavailable", "can_load": False, "detail": detail,
        "models": [{"id": key, "label": label, "state": "unavailable", "detail": None}
                   for key, label in LABELS.items()],
    }


class ModelControlClient:
    def __init__(self, client, settings):
        self.client = client
        self.url = settings.model_control_url
        try:
            self.token = Path(settings.model_control_token_file).read_text().strip()
        except (OSError, ValueError):
            self.token = ""

    async def request(self, *, load=False):
        if not self.url or len(self.token) < 32:
            return (409 if load else 200), unavailable()
        try:
            response = await self.client.request(
                "POST" if load else "GET", self.url + ("/load" if load else "/status"),
                headers={"Authorization": f"Bearer {self.token}"}, timeout=20,
            )
            allowed = {200, 202, 409} if load else {200}
            if response.status_code not in allowed or len(response.content) > 16_384:
                return (409 if load else 200), unavailable()
            snapshot = ModelSnapshot.model_validate(response.json())
            if [model.id for model in snapshot.models] != list(LABELS):
                return (409 if load else 200), unavailable()
            return response.status_code, snapshot.model_dump()
        except (httpx.HTTPError, ValidationError, ValueError):
            # Do not expose Docker responses, paths or the control token to the browser.
            return (409 if load else 200), unavailable()
