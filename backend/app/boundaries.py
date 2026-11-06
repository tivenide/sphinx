"""Same-origin writes and an actual byte limit before JSON parsing, including chunked requests."""

from starlette.responses import JSONResponse
from starlette.exceptions import HTTPException

UPLOAD_PATH = "/api/ingestion/documents"
UPLOAD_REQUEST_LIMIT = 21 * 1024 * 1024


class HttpBoundary:
    def __init__(self, app, origins: frozenset[str]):
        self.app = app
        self.origins = origins

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_receive = receive
        if scope["method"] not in {"GET", "HEAD", "OPTIONS"}:
            headers = dict(scope["headers"])
            if headers.get(b"origin", b"").decode("latin-1") not in self.origins:
                return await JSONResponse({"detail": "Die Anfrage stammt nicht von dieser Anwendung."},
                                          status_code=403)(scope, receive, send)
            content_length = headers.get(b"content-length", b"0")
            upload = scope["method"] == "POST" and scope["path"] == UPLOAD_PATH
            limit = UPLOAD_REQUEST_LIMIT if upload else 8192
            if not content_length.isdigit() or len(content_length) > 10 or int(content_length) > limit:
                return await JSONResponse({"detail": "Die Anfrage ist zu groß."}, status_code=413)(scope, receive, send)
            if upload:
                # Let the endpoint authenticate before reading/spooling any upload bytes.
                received = 0

                async def limited_receive():
                    nonlocal received
                    event = await receive()
                    received += len(event.get("body", b""))
                    if received > limit:
                        raise HTTPException(413, "Die Anfrage ist zu groß.")
                    return event

                request_receive = limited_receive
            else:
                request_receive = await self.buffer_body(scope, receive, send)
                if request_receive is None:
                    return

        async def response_send(message):
            if message["type"] == "http.response.start":
                message["headers"] = [*message.get("headers", []),
                                      (b"cache-control", b"no-store"), (b"x-content-type-options", b"nosniff")]
            await send(message)

        await self.app(scope, request_receive, response_send)

    @staticmethod
    async def buffer_body(scope, receive, send):
        body = bytearray()
        while True:
            event = await receive()
            if event["type"] == "http.disconnect":
                return
            chunk = event.get("body", b"")
            if len(body) + len(chunk) > 8192:
                return await JSONResponse({"detail": "Die Anfrage ist zu groß."}, status_code=413)(scope, receive, send)
            body.extend(chunk)
            if not event.get("more_body", False):
                break
        delivered = False

        async def buffered_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        return buffered_receive
