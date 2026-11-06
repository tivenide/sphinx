"""Admin-only document inventory and persistent upload receipts, without model calls."""

import asyncio
from datetime import datetime, timezone

import asyncpg
from starlette.concurrency import run_in_threadpool


DOCUMENT_COLUMNS = """
    d.id::text AS document_id, d.sha256, d.filename, d.created_at,
    d.chunk_count AS expected_chunks,
    (SELECT count(*) FROM sphinx_ingestion.document_chunks c WHERE c.document_id=d.id) AS chunks
"""


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


async def document_overview(database, pool, *, limit, documents_offset, attempts_offset):
    attempts = await run_in_threadpool(database.list_ingestion, limit, attempts_offset)
    documents = {"items": [], "total": None, "offset": documents_offset}
    matches = {}
    available = False
    if pool is not None:
        try:
            async with asyncio.timeout(5):
                async with pool.acquire(timeout=2) as connection:
                    # Count, page and reconciliation all describe one DB snapshot.
                    async with connection.transaction(isolation="repeatable_read", readonly=True):
                        total = await connection.fetchval("SELECT count(*) FROM sphinx_ingestion.documents")
                        rows = await connection.fetch(f"""SELECT {DOCUMENT_COLUMNS}
                            FROM sphinx_ingestion.documents d ORDER BY d.created_at DESC, d.id DESC
                            LIMIT $1 OFFSET $2""", limit, documents_offset)
                        digests = list({row["sha256"] for row in attempts["items"] if row["sha256"]})
                        matched = await connection.fetch(f"""SELECT {DOCUMENT_COLUMNS}
                            FROM sphinx_ingestion.documents d WHERE d.sha256=ANY($1::text[])""", digests) if digests else []
            documents["total"] = total
            for row in rows:
                item = dict(row)
                item.pop("sha256")
                item["created_at"] = item["created_at"].isoformat()
                item["status"] = "stored" if item["chunks"] == item["expected_chunks"] and item["chunks"] > 0 else "incomplete"
                documents["items"].append(item)
            matches = {row["sha256"]: row["document_id"] for row in matched
                       if row["chunks"] == row["expected_chunks"] and row["chunks"] > 0}
            available = True
        except (TimeoutError, OSError, asyncpg.PostgresError, asyncpg.InterfaceError):
            # Preserve the journal even when the independent knowledge DB is down.
            pass

    for item in attempts["items"]:
        item["stored_document_id"] = matches.get(item.pop("sha256"))
        item["started_at"] = timestamp(item["started_at"])
        item["finished_at"] = timestamp(item["finished_at"])
    return {"documents_available": available, "documents": documents, "attempts": attempts}
