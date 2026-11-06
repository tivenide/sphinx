import asyncio
import hashlib
from email import policy
from email.parser import BytesParser
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from app.boundaries import UPLOAD_PATH, UPLOAD_REQUEST_LIMIT
from app.config import Settings
from app.ingestion import MAX_PDF_BYTES
from app.main import create_app
from app.security import COOKIE_NAME, DUMMY_HASH

ORIGIN = "https://localhost:6500"
TOKEN = "private-ingestion-token-" + "x" * 32
PDF = b"%PDF-1.7\nExact bytes\x00\xff\n%%EOF\n"


@pytest.fixture
def ingestion(tmp_path):
    secret = tmp_path / "webhook-token"
    secret.write_text(TOKEN)
    settings = Settings(database=str(tmp_path / "users.sqlite3"), ingestion_webhook_token_file=str(secret))
    app = create_app(settings)
    calls = []
    upstream = {"status": 201, "body": {"status": "ingested", "document_id": str(uuid4()),
                                         "chunks": 2, "stored": True}}

    async def handle(request):
        calls.append((request, await request.aread()))
        if "error" in upstream:
            raise upstream["error"]
        if "content" in upstream:
            return httpx.Response(upstream["status"], content=upstream["content"])
        return httpx.Response(upstream["status"], json=upstream["body"])

    with TestClient(app, base_url=ORIGIN, headers={"origin": ORIGIN}) as client:
        admin = app.state.db.create_user("admin", DUMMY_HASH, "admin")
        session = app.state.db.new_session(admin["id"], 3600, DUMMY_HASH)
        client.cookies.set(COOKIE_NAME, session)
        upstream_client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
        app.state.ingestion.client = upstream_client
        try:
            yield app, client, calls, upstream
        finally:
            client.portal.call(upstream_client.aclose)


def upload(client, pdf=PDF, filename="test.pdf"):
    return client.post(UPLOAD_PATH, files={"file": (filename, pdf, "application/pdf")})


@pytest.mark.parametrize("status, code", [("ingested", 201), ("already_exists", 200)])
def test_exact_pdf_hash_streamed_and_committed_result(ingestion, status, code):
    app, client, calls, upstream = ingestion
    upstream["status"] = code
    upstream["body"]["status"] = status
    # Cross Starlette's spool threshold to exercise disk reads and cleanup.
    pdf = PDF + b"a" * (1024 * 1024)
    response = upload(client, pdf, 'Prüfung "A".pdf')
    assert response.status_code == code, response.text
    assert response.json() == upstream["body"]
    request, body = calls[0]
    assert str(request.url) == "http://n8n:5678/webhook/sphinx-ingest"
    assert request.headers["X-Ingestion-Token"] == TOKEN
    assert int(request.headers["content-length"]) == len(body)
    assert request.extensions["timeout"]["read"] == 310
    message = BytesParser(policy=policy.default).parsebytes(
        f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + body)
    parts = {part.get_param("name", header="content-disposition"): part for part in message.iter_parts()}
    assert set(parts) == {"sha256", "file"}
    assert parts["sha256"].get_payload(decode=True).decode() == hashlib.sha256(pdf).hexdigest()
    assert parts["file"].get_payload(decode=True) == pdf
    assert parts["file"].get_content_type() == "application/pdf"
    assert "no-store" in response.headers["cache-control"]
    assert not app.state.ingestion.busy


@pytest.mark.asyncio
@pytest.mark.parametrize("who,origin,expected", [("none", ORIGIN, 401), ("user", ORIGIN, 403),
                                                  ("admin", "https://evil.example", 403)])
async def test_auth_and_origin_before_any_body_read(ingestion, who, origin, expected):
    app, client, calls, _ = ingestion
    cookie = client.cookies.get(COOKIE_NAME)
    if who == "user":
        user = app.state.db.create_user("viewer", DUMMY_HASH, "user")
        cookie = app.state.db.new_session(user["id"], 3600, DUMMY_HASH)
    headers = [(b"origin", origin.encode()), (b"content-type", b"multipart/form-data; boundary=x")]
    if who != "none":
        headers.append((b"cookie", f"{COOKIE_NAME}={cookie}".encode()))
    sent = []

    async def receive():
        pytest.fail("Unauthenticated request body was read")

    async def send(event):
        sent.append(event)

    await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
               "scheme": "https", "path": UPLOAD_PATH, "raw_path": UPLOAD_PATH.encode(),
               "query_string": b"", "headers": headers, "client": ("127.0.0.1", 123),
               "server": ("localhost", 6500)}, receive, send)
    assert sent[0]["status"] == expected
    assert not calls


@pytest.mark.parametrize("pdf,filename", [(b"", "test.pdf"), (b"fake PDF", "test.pdf"),
                                          (PDF, "test.txt"), (PDF, "../test.pdf"),
                                          (PDF, "x" * 256 + ".pdf")])
def test_invalid_pdf_rejected_without_forwarding(ingestion, pdf, filename):
    app, client, calls, _ = ingestion
    assert upload(client, pdf, filename).status_code == 400
    assert not calls and not app.state.ingestion.busy


def test_single_file_only_and_no_client_hash(ingestion):
    _, client, calls, _ = ingestion
    assert client.post(UPLOAD_PATH, files=[("file", ("a.pdf", PDF)), ("file", ("b.pdf", PDF))]).status_code == 400
    assert client.post(UPLOAD_PATH, files={"wrong": ("a.pdf", PDF)}).status_code == 400
    assert client.post(UPLOAD_PATH, files={"file": ("a.pdf", PDF)}, data={"sha256": "forged"}).status_code == 400
    assert client.post(UPLOAD_PATH, json={"url": "http://private"}).status_code == 400
    assert client.post(UPLOAD_PATH + "?url=http://private", files={"file": ("a.pdf", PDF)}).status_code == 400
    assert not calls


def test_actual_and_declared_upload_limits_and_other_requests_unchanged(ingestion):
    app, client, calls, _ = ingestion
    assert upload(client, PDF + b"x" * MAX_PDF_BYTES).status_code == 413
    assert client.post(UPLOAD_PATH, content=b"", headers={"content-length": str(UPLOAD_REQUEST_LIMIT + 1)}).status_code == 413
    assert client.post(UPLOAD_PATH, content=b"", headers={"content-length": "9" * 5000}).status_code == 413
    # A misleading Content-Length cannot bypass counting bytes actually received.
    assert client.post(UPLOAD_PATH, content=b"x" * (UPLOAD_REQUEST_LIMIT + 1),
                       headers={"content-length": "1", "content-type": "multipart/form-data; boundary=x"}).status_code == 413
    assert client.post("/api/auth/login", content=b"x" * 8193).status_code == 413
    assert not calls and not app.state.ingestion.busy


@pytest.mark.parametrize("code", [400, 413, 422, 401, 403, 404, 429, 500, 502, 503])
def test_upstream_errors_are_sanitized(ingestion, code):
    _, client, calls, upstream = ingestion
    upstream.update(status=code, content=f"database error secret={TOKEN}".encode())
    response = upload(client)
    assert response.status_code == (code if code in {400, 413, 422} else 503)
    assert TOKEN not in response.text and "database" not in response.text
    assert len(calls) == 1


@pytest.mark.parametrize("change", [
    {"stored": False}, {"stored": "true"}, {"stored": 1}, {"document_id": "not-a-uuid"},
    {"chunks": 0}, {"chunks": True}, {"chunks": 1001}, {"status": "accepted"}, {"status": "already_exists"},
])
def test_only_valid_committed_upstream_results_are_acknowledged(ingestion, change):
    _, client, _, upstream = ingestion
    upstream["body"].update(change)
    assert upload(client).status_code == 503


@pytest.mark.parametrize("content", [b"not json", b"{}", b"x" * 8193], ids=["not-json", "empty-object", "too-large"])
def test_malformed_or_oversized_upstream_response(ingestion, content):
    _, client, _, upstream = ingestion
    upstream["content"] = content
    assert upload(client).status_code == 503


@pytest.mark.parametrize("error,expected", [(httpx.ConnectError("private hostname"), 503),
                                             (httpx.ReadTimeout("private token"), 504)])
def test_connection_failure_timeout_and_temporary_file_cleanup(ingestion, monkeypatch, error, expected):
    import starlette.formparsers as parser_module
    app, client, _, upstream = ingestion
    real_spool = parser_module.SpooledTemporaryFile
    files = []

    def spool(*args, **kwargs):
        file = real_spool(*args, **kwargs)
        files.append(file)
        return file

    monkeypatch.setattr(parser_module, "SpooledTemporaryFile", spool)
    upstream["error"] = error
    assert upload(client, PDF + b"x" * (1024 * 1024)).status_code == expected
    assert files and all(file.closed for file in files)
    assert not app.state.ingestion.busy


def test_truncated_and_malformed_multipart_closes_temporary_files(ingestion, monkeypatch):
    import starlette.formparsers as parser_module
    app, client, calls, _ = ingestion
    real_spool = parser_module.SpooledTemporaryFile
    files = []

    def spool(*args, **kwargs):
        file = real_spool(*args, **kwargs)
        files.append(file)
        return file

    monkeypatch.setattr(parser_module, "SpooledTemporaryFile", spool)
    body = b'--x\r\nContent-Disposition: form-data; name="file"; filename="a.pdf"\r\n\r\n' + PDF
    assert client.post(UPLOAD_PATH, content=body,
                       headers={"content-type": "multipart/form-data; boundary=x"}).status_code == 400
    assert files and all(file.closed for file in files)
    assert client.post(UPLOAD_PATH, content=b"broken", headers={"content-type": "multipart/form-data"}).status_code == 400
    assert not calls and not app.state.ingestion.busy


def test_busy_or_unconfigured_import_does_not_read_or_forward(ingestion):
    app, client, calls, _ = ingestion
    app.state.ingestion.busy = True
    assert upload(client).status_code == 503
    app.state.ingestion.busy = False
    app.state.ingestion.token = ""
    assert upload(client).status_code == 503
    assert not calls


@pytest.mark.asyncio
async def test_stalled_upload_times_out_releases_slot_and_closes_file(ingestion, monkeypatch):
    import app.ingestion as module
    import starlette.formparsers as parser_module
    app, client, calls, _ = ingestion
    monkeypatch.setattr(module, "UPLOAD_TIMEOUT", 0.05)
    real_spool = parser_module.SpooledTemporaryFile
    files = []

    def spool(*args, **kwargs):
        file = real_spool(*args, **kwargs)
        files.append(file)
        return file

    monkeypatch.setattr(parser_module, "SpooledTemporaryFile", spool)
    prefix = b'--x\r\nContent-Disposition: form-data; name="file"; filename="a.pdf"\r\n\r\n' + PDF
    sent = []
    received = False

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": prefix, "more_body": True}
        await asyncio.sleep(60)

    async def send(event):
        sent.append(event)

    cookie = client.cookies.get(COOKIE_NAME)
    await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST",
               "scheme": "https", "path": UPLOAD_PATH, "raw_path": UPLOAD_PATH.encode(),
               "query_string": b"", "headers": [(b"origin", ORIGIN.encode()),
                   (b"cookie", f"{COOKIE_NAME}={cookie}".encode()),
                   (b"content-type", b"multipart/form-data; boundary=x")],
               "client": ("127.0.0.1", 123), "server": ("localhost", 6500)}, receive, send)
    assert sent[0]["status"] == 504
    assert files and all(file.closed for file in files)
    assert not calls and not app.state.ingestion.busy


def test_ingestion_settings_environment(monkeypatch):
    monkeypatch.setenv("INGESTION_WEBHOOK_URL", "http://n8n:5678/webhook/another")
    monkeypatch.setenv("INGESTION_WEBHOOK_TOKEN_FILE", "/run/secrets/ingestion_webhook_token")
    settings = Settings.from_env()
    assert settings.ingestion_webhook_url == "http://n8n:5678/webhook/another"
    assert settings.ingestion_webhook_token_file == "/run/secrets/ingestion_webhook_token"
