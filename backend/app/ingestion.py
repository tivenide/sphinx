"""Bounded, authenticated PDF handoff; n8n owns extraction and persistence."""

import asyncio
import hashlib
import json
import secrets
from pathlib import Path
from typing import Literal
from uuid import UUID

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.responses import JSONResponse

MAX_PDF_BYTES = 20 * 1024 * 1024
CHUNK_SIZE = 64 * 1024
UPLOAD_TIMEOUT = 120
UNAVAILABLE = "Der PDF-Import ist gerade nicht erreichbar. Bitte später erneut versuchen."
INVALID_UPLOAD = "Bitte genau eine gültige PDF-Datei im Feld file auswählen."
CONTENT_ERRORS = {
    400: INVALID_UPLOAD,
    413: "Die PDF ist zu groß oder enthält zu viel Text (maximal 20 MiB).",
    422: "Die PDF konnte nicht verarbeitet werden. Bitte eine lesbare PDF mit Text verwenden.",
}


class PdfParser(MultiPartParser):
    """Keep multipart headers/files bounded and reject truncated uploads."""

    def __init__(self, request):
        super().__init__(request.headers, request.stream(), max_files=1, max_fields=0)
        self.complete = False
        self.header_bytes = 0
        self.file_bytes = 0

    def on_header_field(self, data, start, end):
        self.check_header(end - start)
        super().on_header_field(data, start, end)

    def on_header_value(self, data, start, end):
        self.check_header(end - start)
        super().on_header_value(data, start, end)

    def check_header(self, size):
        self.header_bytes += size
        if self.header_bytes > 8192:
            raise MultiPartException("Multipart headers too large")

    def on_part_data(self, data, start, end):
        self.file_bytes += end - start
        if self.file_bytes > MAX_PDF_BYTES:
            raise HTTPException(413, CONTENT_ERRORS[413])
        super().on_part_data(data, start, end)

    def on_end(self):
        self.complete = True

    async def parse(self):
        try:
            form = await super().parse()
            if not self.complete:
                raise MultiPartException("Incomplete multipart body")
            return form
        except BaseException:
            # Starlette closes parser failures; also close unfinished parts, which
            # have not entered FormData yet when the final boundary is missing.
            for file in self._files_to_close_on_error:
                await run_in_threadpool(file.close)
            raise


class StoredDocument(BaseModel):
    model_config = ConfigDict(strict=True)
    status: Literal["ingested", "already_exists"]
    document_id: UUID
    chunks: int = Field(ge=1, le=1000)
    stored: bool


class IngestionClient:
    def __init__(self, client, settings, database):
        self.client = client
        self.database = database
        self.url = settings.ingestion_webhook_url
        self.busy = False
        try:
            self.token = Path(settings.ingestion_webhook_token_file).read_text().strip()
        except (OSError, ValueError):
            self.token = ""

    async def upload(self, request):
        if not self.url or len(self.token) < 32:
            raise HTTPException(503, UNAVAILABLE)
        if self.busy:
            raise HTTPException(503, "Ein PDF-Import läuft bereits. Bitte danach erneut versuchen.")
        if (request.query_params or
                request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "multipart/form-data"):
            raise HTTPException(400, INVALID_UPLOAD)
        # One worker, with no await between checking and admission: no upload queue
        # and at most one temporary PDF, independently of concurrent admin sessions.
        self.busy = True
        form = None
        attempt_id = None
        outcome = "unconfirmed"
        detail = "Die Verarbeitung wurde unterbrochen. Das Ergebnis ist nicht bestätigt."
        document_id = chunks = None
        try:
            async with asyncio.timeout(UPLOAD_TIMEOUT):
                form = await PdfParser(request).parse()
                parts = form.multi_items()
                if len(parts) != 1 or parts[0][0] != "file" or not isinstance(parts[0][1], UploadFile):
                    raise HTTPException(400, INVALID_UPLOAD)
                upload = parts[0][1]
                name = upload.filename or ""
                if (not name.strip() or len(name) > 255 or not name.lower().endswith(".pdf")
                        or any(ord(c) < 32 or ord(c) == 127 or c in "/\\" for c in name)):
                    raise HTTPException(400, INVALID_UPLOAD)
                # The INSERT may commit before its thread returns. Retain its ID
                # on cancellation so the receipt cannot stay "processing" forever.
                registration = asyncio.create_task(run_in_threadpool(self.database.begin_ingestion, name))
                try:
                    attempt_id = await asyncio.shield(registration)
                except asyncio.CancelledError:
                    attempt_id = await registration
                    raise
                if not upload.size or await upload.read(5) != b"%PDF-":
                    raise HTTPException(400, INVALID_UPLOAD)
                await upload.seek(0)
                digest = hashlib.sha256()
                while chunk := await upload.read(CHUNK_SIZE):
                    digest.update(chunk)
                await upload.seek(0)
                await run_in_threadpool(self.database.ingestion_hash, attempt_id, digest.hexdigest())
            response = await self.forward(upload, digest.hexdigest())
            result = json.loads(response.body)
            outcome, detail = result["status"], None
            document_id, chunks = result["document_id"], result["chunks"]
            return response
        except TimeoutError:
            outcome, detail = "failed", "Das Hochladen dauert zu lange. Bitte erneut versuchen."
            raise HTTPException(504, "Das Hochladen dauert zu lange. Bitte erneut versuchen.") from None
        except MultiPartException:
            outcome, detail = "failed", INVALID_UPLOAD
            raise HTTPException(400, INVALID_UPLOAD) from None
        except StarletteHTTPException as error:
            outcome = "failed" if error.status_code in {400, 413, 422} else "unconfirmed"
            detail = error.detail if error.status_code in {400, 413, 422, 503, 504} else INVALID_UPLOAD
            if error.status_code in {400, 413, 422, 503, 504}:
                raise
            raise HTTPException(400, INVALID_UPLOAD) from None
        except OSError:
            detail = UNAVAILABLE
            raise HTTPException(503, UNAVAILABLE) from None
        finally:
            try:
                try:
                    if attempt_id is not None:
                        await run_in_threadpool(self.database.finish_ingestion, attempt_id, outcome, detail, document_id, chunks)
                finally:
                    if form is not None:
                        await form.close()
            finally:
                self.busy = False

    async def forward(self, upload, digest):
        boundary = secrets.token_hex(24)
        # Quoting is the only filename transformation; the PDF bytes are unchanged.
        filename = upload.filename.replace('"', "%22")
        prefix = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="sha256"\r\n\r\n{digest}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            'Content-Type: application/pdf\r\n\r\n'
        ).encode("utf-8")
        suffix = f"\r\n--{boundary}--\r\n".encode("ascii")

        async def content():
            yield prefix
            while chunk := await upload.read(CHUNK_SIZE):
                yield chunk
            yield suffix

        try:
            async with asyncio.timeout(310):
                async with self.client.stream(
                    "POST", self.url, content=content(),
                    headers={"X-Ingestion-Token": self.token,
                             "Content-Type": f"multipart/form-data; boundary={boundary}",
                             "Content-Length": str(len(prefix) + upload.size + len(suffix))},
                    timeout=httpx.Timeout(310, connect=5, write=30, pool=5),
                    follow_redirects=False,
                ) as response:
                    if response.status_code in CONTENT_ERRORS:
                        raise HTTPException(response.status_code, CONTENT_ERRORS[response.status_code])
                    if response.status_code not in {200, 201}:
                        raise HTTPException(503, UNAVAILABLE)
                    body = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        if len(body) + len(chunk) > 8192:
                            raise HTTPException(503, UNAVAILABLE)
                        body.extend(chunk)
                    result = StoredDocument.model_validate_json(body)
                    expected = 201 if result.status == "ingested" else 200
                    if result.stored is not True or response.status_code != expected:
                        raise HTTPException(503, UNAVAILABLE)
                    return JSONResponse(result.model_dump(mode="json"), status_code=expected)
        except (httpx.TimeoutException, TimeoutError):
            raise HTTPException(504, "Der PDF-Import dauert zu lange. Bitte später erneut versuchen.") from None
        except (httpx.HTTPError, ValidationError, ValueError):
            # Never return n8n internals, database errors or credential details.
            raise HTTPException(503, UNAVAILABLE) from None
