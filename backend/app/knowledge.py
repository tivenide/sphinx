"""Small, read-only pgvector retrieval using the ingestion model already in Ollama."""

import asyncio
import json
import logging
import math
import re
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from typing import Literal

import asyncpg
import httpx

from .config import Settings

EMBEDDING_MODEL = "bge-m3:latest"
# Also enforced by database/retrieval.sql. A model change needs explicit re-embedding.
EMBEDDING_REVISION = "7907646426070047a77226ac3e684fbbe8410524f7b4a74d02837e43f2146bab"
RETRIEVAL_TIMEOUT = 15.0
FAILURE_BACKOFF_SECONDS = 30.0
MAX_PASSAGES = 6
MAX_CONTEXT_CHARS = 6000
MAX_RESPONSE_BYTES = 256 * 1024
LOGGER = logging.getLogger(__name__)

SEARCH_SQL = """
SELECT d.id::text AS document_id, d.filename, c.chunk_index, c.content,
       1 - (c.embedding <=> $1::text::public.vector(1024)) AS score
FROM sphinx_ingestion.document_chunks c
JOIN sphinx_ingestion.documents d ON d.id = c.document_id
WHERE d.embedding_model = $2 AND d.embedding_revision = $3
ORDER BY c.embedding <=> $1::text::public.vector(1024), d.id, c.chunk_index
LIMIT 12
"""


@dataclass(frozen=True)
class Passage:
    document_id: str
    chunk_index: int
    filename: str
    text: str
    score: float


@dataclass(frozen=True)
class KnowledgeResult:
    status: Literal["ok", "empty", "no_match", "unavailable"]
    passages: tuple[Passage, ...] = ()


def search_question(question: str, history: list[dict[str, str]]) -> str:
    """Resolve short follow-ups using user text, never earlier model claims."""
    question = question.strip()[:3000]
    if len(question) < 160 and re.search(
        r"\b(und|das|dazu|dabei|damit|dafür|diese\w*|es|sie|er|dort|dessen)\b", question, re.I
    ):
        previous = next((item["content"] for item in reversed(history[-12:])
                         if item.get("role") == "user" and isinstance(item.get("content"), str)), "")
        if previous:
            return previous[:800] + "\n" + question
    return question


def select_passages(rows, minimum: float) -> tuple[Passage, ...]:
    selected: list[Passage] = []
    seen: set[str] = set()
    for row in rows:
        text = row["content"].strip()
        if not math.isfinite(row["score"]) or row["score"] < minimum or text in seen:
            continue
        seen.add(text)
        selected.append(Passage(row["document_id"], row["chunk_index"], row["filename"], text, row["score"]))
        if len(selected) == MAX_PASSAGES:
            break
    # Remove the ingestion overlap only when both adjacent sections were selected.
    originals = {(p.document_id, p.chunk_index): p.text for p in selected}
    result = []
    remaining = MAX_CONTEXT_CHARS
    for passage in selected:
        text = passage.text
        previous = originals.get((passage.document_id, passage.chunk_index - 1), "")
        for overlap in range(min(len(previous), len(text)), 39, -1):
            if previous.endswith(text[:overlap]):
                text = text[overlap:].lstrip()
                break
        text = text[:remaining]
        if text:
            result.append(Passage(passage.document_id, passage.chunk_index, passage.filename, text, passage.score))
            remaining -= len(text)
    return tuple(result)


class KnowledgeRetriever:
    def __init__(self, client: httpx.AsyncClient, settings: Settings):
        self.client = client
        self.settings = settings
        self.pool: asyncpg.Pool | None = None
        self._retry_after = 0.0

    async def start(self) -> None:
        if not self.settings.knowledge_db_password_file:
            return
        password = await asyncio.to_thread(Path(self.settings.knowledge_db_password_file).read_text)
        if not password.strip():
            raise ValueError("Empty retrieval database password")
        # No connection or model loading at startup: an absent optional DB cannot block login.
        self.pool = await asyncpg.create_pool(
            host=self.settings.knowledge_db_host, database="sphinx_knowledge", user="sphinx_retrieve",
            password=password.strip(), min_size=0, max_size=2, timeout=2, command_timeout=2,
            server_settings={"application_name": "sphinx-retrieval", "default_transaction_read_only": "on",
                             "statement_timeout": "2000"},
        )

    async def close(self) -> None:
        if self.pool is not None:
            try:
                async with asyncio.timeout(3):
                    await self.pool.close()
            except TimeoutError:
                self.pool.terminate()

    async def _ollama(self, path: str, payload=None) -> dict:
        async with self.client.stream(
            "POST" if payload is not None else "GET", self.settings.knowledge_ollama_url + path,
            json=payload, timeout=httpx.Timeout(RETRIEVAL_TIMEOUT, connect=2),
        ) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=8192):
                body.extend(chunk)
                if len(body) > MAX_RESPONSE_BYTES:
                    raise ValueError("Oversized embedding response")
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("Invalid embedding response")
        return value

    async def _check_revision(self) -> None:
        models = (await self._ollama("/api/tags")).get("models")
        if not isinstance(models, list) or not any(
            isinstance(model, dict) and model.get("name") == EMBEDDING_MODEL
            and model.get("digest") == EMBEDDING_REVISION for model in models
        ):
            raise ValueError("Embedding revision mismatch")

    async def retrieve(self, question: str, history: list[dict[str, str]]) -> KnowledgeResult:
        if self.pool is None or monotonic() < self._retry_after:
            return KnowledgeResult("unavailable")
        try:
            async with asyncio.timeout(RETRIEVAL_TIMEOUT):
                async with self.pool.acquire(timeout=2) as connection:
                    state = await connection.fetchrow("""
                        SELECT EXISTS (SELECT 1 FROM sphinx_ingestion.document_chunks) AS populated,
                               EXISTS (SELECT 1 FROM sphinx_ingestion.documents
                                       WHERE embedding_model <> $1 OR embedding_revision <> $2) AS incompatible
                    """, EMBEDDING_MODEL, EMBEDDING_REVISION)
                if state["incompatible"]:
                    raise ValueError("Stored embedding revision mismatch")
                if not state["populated"]:
                    return KnowledgeResult("empty")
                await self._check_revision()
                response = await self._ollama("/api/embed", {
                    "model": EMBEDDING_MODEL, "input": search_question(question, history),
                    "truncate": False, "keep_alive": "5m",
                })
                vectors = response.get("embeddings")
                if (response.get("model") != EMBEDDING_MODEL or not isinstance(vectors, list)
                        or len(vectors) != 1 or not isinstance(vectors[0], list) or len(vectors[0]) != 1024):
                    raise ValueError("Invalid query embedding")
                vector = vectors[0]
                if (not all(type(v) in (int, float) and abs(v) <= 3.4028234663852886e38 and math.isfinite(v)
                            for v in vector) or not any(vector)):
                    raise ValueError("Invalid query embedding values")
                # Detect a changed mutable tag across inference, not just before it.
                await self._check_revision()
                async with self.pool.acquire(timeout=2) as connection:
                    rows = await connection.fetch(SEARCH_SQL, json.dumps(vector, allow_nan=False),
                                                  EMBEDDING_MODEL, EMBEDDING_REVISION)
                passages = select_passages(rows, self.settings.knowledge_min_score)
                return KnowledgeResult("ok" if passages else "no_match", passages)
        except (TimeoutError, httpx.HTTPError, asyncpg.PostgresError, asyncpg.InterfaceError, OSError,
                ValueError, TypeError, KeyError) as error:
            # Voice turns are serialized; following turns skip failed dependencies
            # until the next retry window. Empty/no-match and cancellations never enter it.
            self._retry_after = monotonic() + FAILURE_BACKOFF_SECONDS
            # No questions, excerpts, credentials or upstream bodies in logs.
            LOGGER.warning("Knowledge retrieval unavailable (%s)", type(error).__name__)
            return KnowledgeResult("unavailable")
