"""Retrieval contracts with synthetic evidence; no model or production DB calls."""

import asyncio
import json
from contextlib import asynccontextmanager

import asyncpg
import httpx
import pytest

from app.config import Settings
from app.knowledge import (
    EMBEDDING_MODEL, EMBEDDING_REVISION, FAILURE_BACKOFF_SECONDS,
    KnowledgeRetriever, search_question, select_passages,
)


class Database:
    def __init__(self, *, populated=True, incompatible=False, rows=(), error=None):
        self.state = {"populated": populated, "incompatible": incompatible}
        self.rows = rows
        self.error = error
        self.searches = []
        self.acquired = 0
        self.acquisitions = 0

    @asynccontextmanager
    async def acquire(self, **kwargs):
        self.acquired += 1
        self.acquisitions += 1
        try:
            if self.error:
                raise self.error
            yield self
        finally:
            self.acquired -= 1

    async def fetchrow(self, sql, *args):
        return self.state

    async def fetch(self, sql, *args):
        self.searches.append((sql, args))
        return self.rows


def section(text="Die Wartung dauert fünfzehn Minuten.", index=0, score=0.8, document="doc-1"):
    return {"document_id": document, "filename": "Betrieb.pdf", "chunk_index": index,
            "content": text, "score": score}


def tags(revision=EMBEDDING_REVISION):
    return httpx.Response(200, json={"models": [{"name": EMBEDDING_MODEL, "digest": revision}]})


async def retrieve(database, handler, question="Wie lange dauert die Wartung?", history=None):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        retriever = KnowledgeRetriever(client, Settings())
        retriever.pool = database
        return await retriever.retrieve(question, history or [])


@pytest.mark.asyncio
async def test_query_uses_ingestion_model_and_parameterized_search_with_bounded_followup():
    database = Database(rows=[section(), section("Andere Information", score=0.2)])
    requests = []

    async def handler(request):
        requests.append(request)
        if request.url.path == "/api/tags":
            return tags()
        body = json.loads(request.content)
        assert body == {"model": EMBEDDING_MODEL,
                        "input": "Wie funktioniert die Wartung?\nUnd wie lange dauert das?",
                        "truncate": False, "keep_alive": "5m"}
        return httpx.Response(200, json={"model": EMBEDDING_MODEL, "embeddings": [[0.01] * 1024]})

    result = await retrieve(database, handler, "Und wie lange dauert das?", [
        {"role": "user", "content": "Wie funktioniert die Wartung?"},
        {"role": "assistant", "content": "Unbelegte frühere Behauptung"},
    ])
    assert result.status == "ok"
    assert [p.text for p in result.passages] == ["Die Wartung dauert fünfzehn Minuten."]
    assert result.passages[0].filename == "Betrieb.pdf"
    sql, args = database.searches[0]
    assert json.loads(args[0]) == [0.01] * 1024
    assert args[1:] == (EMBEDDING_MODEL, EMBEDDING_REVISION)
    assert "<=> $1::text::public.vector(1024)" in sql
    assert [r.url.path for r in requests] == ["/api/tags", "/api/embed", "/api/tags"]
    assert database.acquired == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("database,status", [
    (Database(populated=False), "empty"),
    (Database(incompatible=True), "unavailable"),
    (Database(error=asyncpg.CannotConnectNowError("private database details")), "unavailable"),
])
async def test_empty_or_unavailable_database_never_loads_embedding_model(database, status):
    async def unexpected(request):
        pytest.fail("Ollama must not be called")
    result = await retrieve(database, unexpected)
    assert result.status == status
    assert not result.passages and not database.searches and not database.acquired


@pytest.mark.asyncio
@pytest.mark.parametrize("vector", [
    [], [0.01] * 768, [0.0] * 1024,
    [0.01] * 1023 + [True], [0.01] * 1023 + ["0.01"],
    [0.01] * 1023 + [float("nan")], [0.01] * 1023 + [float("inf")],
    [0.01] * 1023 + [1e40], [0.01] * 1023 + [10**400],
])
async def test_invalid_embedding_never_reaches_database(vector):
    database = Database()
    async def handler(request):
        if request.url.path == "/api/tags":
            return tags()
        # Raw content allows deliberately invalid JSON numeric extensions.
        return httpx.Response(200, content=json.dumps({"model": EMBEDDING_MODEL, "embeddings": [vector]}))
    result = await retrieve(database, handler)
    assert result.status == "unavailable" and not database.searches


@pytest.mark.asyncio
@pytest.mark.parametrize("change_after_embedding", [False, True])
async def test_changed_ollama_revision_never_uses_incompatible_vectors(change_after_embedding):
    database = Database()
    embedded = False
    async def handler(request):
        nonlocal embedded
        if request.url.path == "/api/tags":
            return tags("f" * 64 if embedded or not change_after_embedding else EMBEDDING_REVISION)
        embedded = True
        return httpx.Response(200, json={"model": EMBEDDING_MODEL, "embeddings": [[0.01] * 1024]})
    result = await retrieve(database, handler)
    assert result.status == "unavailable" and not database.searches
    assert embedded == change_after_embedding


@pytest.mark.asyncio
async def test_no_relevant_passage_is_distinct_from_provider_failure():
    database = Database(rows=[section(score=0.2)])
    async def handler(request):
        if request.url.path == "/api/tags":
            return tags()
        return httpx.Response(200, json={"model": EMBEDDING_MODEL, "embeddings": [[0.01] * 1024]})
    result = await retrieve(database, handler)
    assert result.status == "no_match" and not result.passages
    failed = await retrieve(database, lambda request: httpx.Response(503, text="private error"))
    assert failed.status == "unavailable" and not failed.passages


@pytest.mark.asyncio
async def test_retrieval_deadline_and_cancellation_release_database_and_do_not_search(monkeypatch):
    database = Database()
    entered = asyncio.Event()
    async def blocked(request):
        entered.set()
        await asyncio.Event().wait()
    task = asyncio.create_task(retrieve(database, blocked))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not database.acquired and not database.searches

    monkeypatch.setattr("app.knowledge.RETRIEVAL_TIMEOUT", 0.01)
    result = await retrieve(database, blocked)
    assert result.status == "unavailable" and not database.acquired and not database.searches


@pytest.mark.asyncio
async def test_oversized_provider_response_is_bounded():
    result = await retrieve(Database(), lambda request: httpx.Response(200, content=b"x" * (256 * 1024 + 1)))
    assert result.status == "unavailable"


def test_context_deduplicates_adjacent_overlap_and_limits_evidence():
    overlap = "Ein gemeinsamer Satz im überlappenden Abschnitt. " * 4
    rows = [section("Erster Abschnitt. " + overlap), section(overlap + "Neue Einzelheiten.", index=1),
            section("Erster Abschnitt. " + overlap, document="duplicate")]
    result = select_passages(rows, 0.45)
    assert len(result) == 2 and result[1].text == "Neue Einzelheiten."
    many = select_passages([section(str(i) + "x" * 999, index=i, document=str(i)) for i in range(20)], 0.45)
    assert len(many) == 6 and sum(len(p.text) for p in many) <= 6000


def test_standalone_question_does_not_reuse_unrelated_history():
    history = [{"role": "user", "content": "Alte Frage zur Buchhaltung"}]
    assert search_question("Wie funktioniert die Wartung?", history) == "Wie funktioniert die Wartung?"
    assert len(search_question("Und wie lange gilt das?", [{"role": "user", "content": "x" * 8000}])) < 1000


@pytest.mark.asyncio
async def test_optional_database_is_lazy_and_shutdown_closes_pool(tmp_path, monkeypatch):
    secret = tmp_path / "password"
    secret.write_text("test-secret\n")
    seen = {}
    class Pool:
        async def close(self):
            seen["closed"] = True
    async def create_pool(**kwargs):
        seen.update(kwargs)
        return Pool()
    monkeypatch.setattr("app.knowledge.asyncpg.create_pool", create_pool)
    async with httpx.AsyncClient() as client:
        unconfigured = KnowledgeRetriever(client, Settings())
        await unconfigured.start()
        assert (await unconfigured.retrieve("Hallo", [])).status == "unavailable"
        assert not seen
        configured = KnowledgeRetriever(client, Settings(knowledge_db_password_file=str(secret)))
        await configured.start()
        assert seen["min_size"] == 0 and seen["max_size"] == 2
        assert seen["user"] == "sphinx_retrieve" and seen["password"] == "test-secret"
        assert seen["server_settings"]["default_transaction_read_only"] == "on"
        await configured.close()
        assert seen["closed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["database", "ollama", "revision"])
async def test_failure_backoff_skips_all_io_then_recovers_and_can_fail_again(monkeypatch, caplog, failure):
    clock = [100.0]
    monkeypatch.setattr("app.knowledge.monotonic", lambda: clock[0])
    database = Database(rows=[section()])
    broken = True
    requests = []

    async def handler(request):
        requests.append(request.url.path)
        if broken and failure == "ollama":
            # The quiet period starts after the failed request, not at its start.
            clock[0] += 10
            raise httpx.ReadTimeout("private upstream details")
        if request.url.path == "/api/tags":
            return tags("f" * 64 if broken and failure == "revision" else EMBEDDING_REVISION)
        return httpx.Response(200, json={"model": EMBEDDING_MODEL, "embeddings": [[0.01] * 1024]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        retriever = KnowledgeRetriever(client, Settings())
        retriever.pool = database
        if failure == "database":
            database.error = asyncpg.CannotConnectNowError("private database details")
        assert (await retriever.retrieve("Erste Frage", [])).status == "unavailable"
        failed_at = clock[0]
        acquisitions, http_calls = database.acquisitions, len(requests)
        for seconds in [0, 1, FAILURE_BACKOFF_SECONDS - 0.01]:
            clock[0] = failed_at + seconds
            result = await retriever.retrieve("Noch eine Frage", [])
            assert result.status == "unavailable" and not result.passages
        assert database.acquisitions == acquisitions and len(requests) == http_calls
        assert len(caplog.records) == 1 and "private" not in caplog.text

        broken = False
        database.error = None
        clock[0] = failed_at + FAILURE_BACKOFF_SECONDS
        recovered = await retriever.retrieve("Neue Frage", [])
        assert recovered.status == "ok" and recovered.passages
        assert len(database.searches) == 1
        # Normal successful turns are not cached.
        assert (await retriever.retrieve("Andere Frage", [])).status == "ok"
        assert len(database.searches) == 2

        broken = True
        if failure == "database":
            database.error = asyncpg.CannotConnectNowError("private database details")
        assert (await retriever.retrieve("Spaeterer Ausfall", [])).status == "unavailable"
        acquisitions, http_calls = database.acquisitions, len(requests)
        assert (await retriever.retrieve("Direkte Folgefrage", [])).status == "unavailable"
        assert database.acquisitions == acquisitions and len(requests) == http_calls
        assert len(caplog.records) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("initial", ["empty", "no_match"])
async def test_empty_or_no_match_does_not_hide_newly_available_documents(initial):
    database = Database(populated=initial != "empty", rows=[])
    async def handler(request):
        if request.url.path == "/api/tags":
            return tags()
        return httpx.Response(200, json={"model": EMBEDDING_MODEL, "embeddings": [[0.01] * 1024]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        retriever = KnowledgeRetriever(client, Settings())
        retriever.pool = database
        assert (await retriever.retrieve("Frage", [])).status == initial
        database.state["populated"] = True
        database.rows = [section()]
        assert (await retriever.retrieve("Frage", [])).status == "ok"


@pytest.mark.asyncio
async def test_user_cancellation_does_not_put_next_turn_into_backoff():
    database = Database(rows=[section()])
    entered = asyncio.Event()
    blocked = True
    async def handler(request):
        if blocked:
            entered.set()
            await asyncio.Event().wait()
        if request.url.path == "/api/tags":
            return tags()
        return httpx.Response(200, json={"model": EMBEDDING_MODEL, "embeddings": [[0.01] * 1024]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        retriever = KnowledgeRetriever(client, Settings())
        retriever.pool = database
        task = asyncio.create_task(retriever.retrieve("Abgebrochene Frage", []))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        blocked = False
        result = await retriever.retrieve("Neue Frage", [])
        assert result.status == "ok" and result.passages
