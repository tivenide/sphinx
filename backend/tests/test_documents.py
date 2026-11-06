"""Document status is backed by receipts and current stored chunks, never inference."""

import asyncio
import hashlib
import io
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import httpx
import pytest
from starlette.datastructures import FormData, UploadFile

from app.database import Database
from app.security import COOKIE_NAME, DUMMY_HASH
from test_ingestion import PDF, TOKEN, ingestion, upload

PATH = "/api/ingestion/documents"
DIGEST = hashlib.sha256(PDF).hexdigest()


class DocumentPool:
    def __init__(self, rows=(), error=None):
        self.rows = list(rows)
        self.error = error
        self.calls = []
        self.transactions = []

    @asynccontextmanager
    async def acquire(self, **kwargs):
        self.calls.append(("acquire", kwargs))
        if self.error:
            raise self.error
        yield self

    @asynccontextmanager
    async def transaction(self, **kwargs):
        self.transactions.append(kwargs)
        yield self

    async def fetchval(self, query):
        self.calls.append((query, ()))
        return len(self.rows)

    async def fetch(self, query, *args):
        self.calls.append((query, args))
        if "ANY(" in query:
            return [row.copy() for row in self.rows if row["sha256"] in args[0]]
        limit, offset = args
        return [row.copy() for row in self.rows[offset:offset + limit]]

    async def close(self):
        pass


def document(filename="Handbuch.pdf", *, chunks=2, expected=2, digest=DIGEST):
    return {"document_id": str(uuid4()), "sha256": digest, "filename": filename,
            "created_at": datetime(2026, 11, 3, 10, 15, tzinfo=timezone.utc),
            "expected_chunks": expected, "chunks": chunks}


def overview(client, **params):
    response = client.get(PATH, params=params)
    assert response.status_code == 200, response.text
    assert "no-store" in response.headers["cache-control"]
    return response.json()


@pytest.mark.parametrize("outcome,http_status,stored_status", [
    ("ingested", 201, "ingested"),
    ("duplicate", 200, "already_exists"),
    ("unreadable", 422, "failed"),
    ("unavailable", 503, "unconfirmed"),
    ("timeout", 504, "unconfirmed"),
    ("malformed", 503, "unconfirmed"),
])
def test_upload_records_only_confirmed_and_sanitized_outcomes(ingestion, outcome, http_status, stored_status):
    app, client, calls, upstream = ingestion
    secret_text = f"private PDF text and credentials: {TOKEN}"
    upstream["body"]["document_text"] = secret_text
    if outcome == "duplicate":
        upstream["status"] = 200
        upstream["body"]["status"] = "already_exists"
    elif outcome in {"unreadable", "unavailable"}:
        upstream.update(status=http_status, content=secret_text.encode())
    elif outcome == "timeout":
        upstream["error"] = httpx.ReadTimeout(secret_text)
    elif outcome == "malformed":
        upstream["content"] = secret_text.encode()

    response = upload(client, filename="Prüfung.pdf")
    assert response.status_code == http_status
    assert len(calls) == 1
    result = overview(client)
    assert result["attempts"]["total"] == 1
    attempt = result["attempts"]["items"][0]
    assert attempt["filename"] == "Prüfung.pdf"
    assert attempt["status"] == stored_status
    assert datetime.fromisoformat(attempt["started_at"]) <= datetime.fromisoformat(attempt["finished_at"])
    assert attempt["stored_document_id"] is None
    if http_status in {200, 201}:
        assert attempt["document_id"] == upstream["body"]["document_id"]
        assert attempt["chunks"] == 2
        assert attempt["detail"] is None
    else:
        assert attempt["document_id"] is None and attempt["chunks"] is None
        assert attempt["detail"] == response.json()["detail"]
    assert DIGEST not in str(result)
    assert TOKEN not in str(result) and secret_text not in str(result)
    saved = app.state.db.list_ingestion(25, 0)["items"][0]
    assert saved["sha256"] == DIGEST


def test_restart_preserves_receipts_and_marks_interrupted_import_as_unconfirmed(tmp_path):
    path = str(tmp_path / "accounts.sqlite3")
    db = Database(path)
    db.initialize()
    successful = db.begin_ingestion("stored.pdf")
    doc_id = str(uuid4())
    db.finish_ingestion(successful, "ingested", document_id=doc_id, chunks=3)
    pending = db.begin_ingestion("interrupted.pdf")
    db.ingestion_hash(pending, DIGEST)
    before = db.list_ingestion(25, 0)["items"]
    assert before[0]["status"] == "processing" and before[0]["finished_at"] is None

    restarted = Database(path)
    restarted.initialize()
    after = restarted.list_ingestion(25, 0)
    assert after["total"] == 2
    interrupted, completed = after["items"]
    assert interrupted["id"] == pending
    assert interrupted["status"] == "unconfirmed"
    assert interrupted["sha256"] == DIGEST and interrupted["finished_at"] is not None
    assert interrupted["detail"]
    assert completed == before[1]
    assert completed["status"] == "ingested" and completed["document_id"] == doc_id
    restarted.initialize()
    assert restarted.list_ingestion(25, 0) == after


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["registration", "forward"])
async def test_cancelled_upload_cannot_leave_a_processing_receipt(ingestion, monkeypatch, stage):
    import app.ingestion as module

    app, _, calls, _ = ingestion
    service = app.state.ingestion
    file = UploadFile(io.BytesIO(PDF), filename="cancelled.pdf", size=len(PDF))
    form = FormData([("file", file)])

    async def parse():
        return form

    monkeypatch.setattr(module, "PdfParser", lambda request: SimpleNamespace(parse=parse))
    reached = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original_begin = app.state.db.begin_ingestion

    def paused_registration(filename):
        # Reproduce a committed INSERT whose result has not reached its awaiter.
        attempt_id = original_begin(filename)
        loop.call_soon_threadsafe(reached.set)
        assert release.wait(5), "Registration thread was not released"
        return attempt_id

    async def forward(upload, digest):
        assert stage == "forward"
        reached.set()
        await asyncio.Event().wait()

    if stage == "registration":
        monkeypatch.setattr(app.state.db, "begin_ingestion", paused_registration)
    monkeypatch.setattr(service, "forward", forward)
    request = SimpleNamespace(query_params={}, headers={"content-type": "multipart/form-data"})
    task = asyncio.create_task(service.upload(request))
    try:
        await asyncio.wait_for(reached.wait(), 5)
        task.cancel()
        # Deliver cancellation before releasing the committed registration.
        await asyncio.sleep(0)
    finally:
        release.set()
        if not task.done() and not task.cancelling():
            task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)

    saved = app.state.db.list_ingestion(25, 0)
    assert saved["total"] == 1
    attempt = saved["items"][0]
    assert attempt["status"] == "unconfirmed"
    assert attempt["finished_at"] is not None and attempt["detail"]
    assert attempt["document_id"] is None and attempt["chunks"] is None
    assert attempt["sha256"] == (DIGEST if stage == "forward" else None)
    assert file.file.closed and not service.busy and not calls


def test_inventory_includes_direct_ingestion_and_checks_actual_chunk_count(ingestion):
    app, client, _, _ = ingestion
    rows = [document("direkt.pdf"), document("fehlend.pdf", chunks=1),
            document("leer.pdf", chunks=0, expected=0), document("zu-viele.pdf", chunks=3)]
    pool = DocumentPool(rows)
    app.state.knowledge.pool = pool
    result = overview(client)
    assert result["documents_available"] is True
    assert result["documents"]["total"] == 4
    assert result["attempts"] == {"items": [], "total": 0, "offset": 0}
    items = result["documents"]["items"]
    assert [item["filename"] for item in items] == [row["filename"] for row in rows]
    assert [item["status"] for item in items] == ["stored", "incomplete", "incomplete", "incomplete"]
    assert [item["chunks"] for item in items] == [2, 1, 0, 3]
    assert all(datetime.fromisoformat(item["created_at"]).tzinfo is not None for item in items)
    assert all("sha256" not in item and "content" not in item for item in items)
    assert pool.transactions == [{"isolation": "repeatable_read", "readonly": True}]


@pytest.mark.parametrize("status", ["unconfirmed", "failed"])
def test_matching_stored_document_does_not_rewrite_attempt_outcome(ingestion, status):
    app, client, _, _ = ingestion
    attempt_id = app.state.db.begin_ingestion("another-name.pdf")
    app.state.db.ingestion_hash(attempt_id, DIGEST)
    app.state.db.finish_ingestion(attempt_id, status, "Ergebnis des einzelnen Versuchs.")
    row = document()
    app.state.knowledge.pool = DocumentPool([row])
    attempt = overview(client)["attempts"]["items"][0]
    assert attempt["status"] == status
    assert attempt["stored_document_id"] == row["document_id"]
    assert attempt["document_id"] is None and attempt["chunks"] is None
    assert attempt["detail"] == "Ergebnis des einzelnen Versuchs."
    assert "sha256" not in attempt
    assert app.state.db.list_ingestion(25, 0)["items"][0]["status"] == status


def test_incomplete_document_is_not_used_to_confirm_an_unknown_attempt(ingestion):
    app, client, _, _ = ingestion
    attempt_id = app.state.db.begin_ingestion("pending.pdf")
    app.state.db.ingestion_hash(attempt_id, DIGEST)
    app.state.db.finish_ingestion(attempt_id, "unconfirmed")
    app.state.knowledge.pool = DocumentPool([document(chunks=1)])
    result = overview(client)
    assert result["documents"]["items"][0]["status"] == "incomplete"
    assert result["attempts"]["items"][0]["status"] == "unconfirmed"
    assert result["attempts"]["items"][0]["stored_document_id"] is None


def test_success_receipt_does_not_invent_a_currently_stored_document(ingestion):
    app, client, _, _ = ingestion
    assert upload(client).status_code == 201
    app.state.knowledge.pool = DocumentPool()
    result = overview(client)
    assert result["documents_available"] is True
    assert result["documents"]["total"] == 0
    assert result["documents"]["items"] == []
    assert result["attempts"]["items"][0]["status"] == "ingested"
    assert result["attempts"]["items"][0]["stored_document_id"] is None


@pytest.mark.parametrize("error", [None, TimeoutError("private"), OSError("private"),
                                    asyncpg.CannotConnectNowError("private"), asyncpg.InterfaceError("private")])
def test_unavailable_database_is_distinct_from_empty_and_preserves_journal(ingestion, error):
    app, client, _, _ = ingestion
    assert upload(client).status_code == 201
    app.state.knowledge.pool = DocumentPool(error=error) if error else None
    result = overview(client)
    assert result["documents_available"] is False
    assert result["documents"] == {"items": [], "total": None, "offset": 0}
    assert result["attempts"]["total"] == 1
    assert result["attempts"]["items"][0]["status"] == "ingested"
    assert result["attempts"]["items"][0]["stored_document_id"] is None
    assert "private" not in str(result)


@pytest.mark.parametrize("role,expected", [(None, 401), ("user", 403)])
def test_document_inventory_requires_admin_before_database_queries(ingestion, role, expected):
    app, client, _, _ = ingestion
    pool = DocumentPool()
    app.state.knowledge.pool = pool
    client.cookies.clear()
    if role:
        user = app.state.db.create_user("viewer", DUMMY_HASH, role)
        token = app.state.db.new_session(user["id"], 3600, DUMMY_HASH)
        client.cookies.set(COOKIE_NAME, token)
    response = client.get(PATH)
    assert response.status_code == expected
    assert pool.calls == []


def test_document_and_attempt_pages_are_independent_and_bounded(ingestion):
    app, client, _, _ = ingestion
    rows = [document(f"doc-{i}.pdf", digest=str(i).zfill(64)) for i in range(6)]
    pool = DocumentPool(rows)
    app.state.knowledge.pool = pool
    for i in range(6):
        app.state.db.begin_ingestion(f"upload-{i}.pdf")
    result = overview(client, limit=2, documents_offset=1, attempts_offset=3)
    assert result["documents"]["total"] == result["attempts"]["total"] == 6
    assert result["documents"]["offset"] == 1
    assert result["attempts"]["offset"] == 3
    assert [row["filename"] for row in result["documents"]["items"]] == ["doc-1.pdf", "doc-2.pdf"]
    assert [row["filename"] for row in result["attempts"]["items"]] == ["upload-2.pdf", "upload-1.pdf"]
    assert all(row["status"] == "processing" for row in result["attempts"]["items"])
    result = overview(client, limit=100, documents_offset=100, attempts_offset=100)
    assert result["documents"]["items"] == result["attempts"]["items"] == []
    assert result["documents"]["total"] == result["attempts"]["total"] == 6


@pytest.mark.parametrize("query", [{"limit": 0}, {"limit": 101}, {"limit": "bad"},
                                    {"documents_offset": -1}, {"attempts_offset": -1},
                                    {"documents_offset": 1_000_001}, {"attempts_offset": 1_000_001}])
def test_invalid_pagination_never_queries_documents(ingestion, query):
    app, client, _, _ = ingestion
    pool = DocumentPool()
    app.state.knowledge.pool = pool
    assert client.get(PATH, params=query).status_code == 422
    assert pool.calls == []
