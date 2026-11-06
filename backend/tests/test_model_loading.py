import httpx
import pytest

from app.config import Settings
from app.model_loading import ModelControlClient, LABELS


def snapshot(state="loading"):
    return {"state": state, "can_load": False, "detail": None,
            "models": [{"id": key, "label": label, "state": state, "detail": None}
                       for key, label in LABELS.items()]}


@pytest.mark.asyncio
async def test_control_client_sends_only_fixed_paths_and_private_token(tmp_path):
    secret = tmp_path / "token"
    secret.write_text("private-control-token-" * 2)
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer " + secret.read_text()
        assert request.content == b""
        return httpx.Response(409 if request.method == "POST" else 200, json=snapshot("unavailable"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = ModelControlClient(http, Settings(model_control_url="http://controller:8082",
                                                   model_control_token_file=str(secret)))
        assert (await client.request())[0] == 200
        code, result = await client.request(load=True)
        assert code == 409
        assert result == snapshot("unavailable")
        assert secret.read_text() not in str(result)
    assert [(r.method, r.url.path) for r in requests] == [("GET", "/status"), ("POST", "/load")]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["network", "unauthorized", "malformed", "wrong_ids"])
async def test_unavailable_controller_fails_closed_without_leaking_response(tmp_path, failure):
    secret = tmp_path / "token"
    secret.write_text("private-control-token-" * 2)

    def handle(request):
        if failure == "network":
            raise httpx.ConnectError("sensitive docker details", request=request)
        if failure == "unauthorized":
            return httpx.Response(401, text="sensitive docker details")
        if failure == "malformed":
            return httpx.Response(200, json={"secret": "sensitive docker details"})
        value = snapshot()
        value["models"] = []
        return httpx.Response(200, json=value)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = ModelControlClient(http, Settings(model_control_url="http://controller:8082",
                                                   model_control_token_file=str(secret)))
        code, result = await client.request(load=True)
        assert code == 409
        assert result["state"] == "unavailable" and result["can_load"] is False
        assert "sensitive" not in str(result)
