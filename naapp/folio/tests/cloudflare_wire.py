"""Cloudflare Stream's API at the wire: a loopback server answering its documented JSON.

Folio's own Cloudflare client sends real HTTP to ``cloudflare_api_base``; only
Cloudflare is replaced, by an app that answers as the API reference documents
(read 2026-11-25):
https://developers.cloudflare.com/api/resources/stream/subresources/direct_upload/methods/create/
and https://developers.cloudflare.com/api/resources/stream/methods/get/.
Every request it receives is kept, so a test can assert what Folio sent.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request
from served_app import served

ACCOUNT_ID = "folio-test-account"
API_TOKEN = "folio-test-stream-token"
STREAM_UID = "stream-uid"
UPLOAD_URL = "https://upload.videodelivery.net/stream-uid"


@dataclass(frozen=True)
class Received:
    method: str
    path: str
    authorization: str | None
    body: Any


@dataclass
class Cloudflare:
    """The API's origin, the state it reports for STREAM_UID, and what it received."""

    origin: str
    state: str = "pendingupload"
    received: list[Received] = field(default_factory=list)

    def settings(self) -> dict[str, str]:
        """Folio's Cloudflare settings for this server."""
        return {
            "cloudflare_api_base": self.origin,
            "cloudflare_account_id": ACCOUNT_ID,
            "cloudflare_stream_api_token": API_TOKEN,
        }


def _envelope(result: dict[str, Any]) -> dict[str, Any]:
    return {"success": True, "errors": [], "messages": [], "result": result}


@asynccontextmanager
async def cloudflare_stream_api() -> AsyncIterator[Cloudflare]:
    app = FastAPI()
    api = Cloudflare(origin="")

    async def keep(request: Request) -> None:
        body = await request.json() if await request.body() else None
        api.received.append(
            Received(request.method, request.url.path, request.headers.get("authorization"), body)
        )

    @app.post("/accounts/{account_id}/stream/direct_upload")
    async def direct_upload(account_id: str, request: Request) -> dict[str, Any]:
        await keep(request)
        return _envelope({"uid": STREAM_UID, "uploadURL": UPLOAD_URL})

    @app.get("/accounts/{account_id}/stream/{uid}")
    async def video(account_id: str, uid: str, request: Request) -> dict[str, Any]:
        await keep(request)
        return _envelope({"uid": uid, "readyToStream": False, "status": {"state": api.state}})

    async with served(app) as origin:
        api.origin = origin
        yield api
