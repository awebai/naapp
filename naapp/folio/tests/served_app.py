"""A Folio app served by real Uvicorn on a loopback port the kernel picks."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import uvicorn


@asynccontextmanager
async def served(app: Any, **config: Any) -> AsyncIterator[str]:
    """Serves ``app`` until the block ends and yields its origin. A server that
    fails to start raises its own error, not a start-up timeout."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        server = uvicorn.Server(
            uvicorn.Config(app, lifespan="off", log_config=None, ws="none", **config)
        )
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if serving.done():
                        await serving
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{listener.getsockname()[1]}"
        finally:
            server.should_exit = True
            await asyncio.wait_for(serving, timeout=10)
