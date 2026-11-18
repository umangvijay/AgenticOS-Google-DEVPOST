"""Serve the workflow worker's actual WebAgent runtime on its internal endpoint."""
import asyncio
import os
from contextlib import asynccontextmanager
from urllib.parse import urlsplit


@asynccontextmanager
async def browser_runtime_server():
    endpoint = os.environ.get("AGENTOS_BROWSER_INTERNAL_URL", "").strip()
    if not endpoint:
        yield None
        return
    if not os.environ.get("AGENTOS_BROWSER_INTERNAL_TOKEN", "").strip():
        raise RuntimeError("Browser worker routing requires an internal token")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "http" or not parsed.hostname or parsed.path not in ("", "/") or parsed.username or parsed.query or parsed.fragment:
        raise RuntimeError("Browser internal URL must be a credential-free HTTP origin")
    from fastapi import FastAPI
    import uvicorn
    from backend.api.routers.browser_viewer import internal_router, get_browser_service
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(internal_router)
    server = uvicorn.Server(uvicorn.Config(app, host=os.environ.get("AGENTOS_BROWSER_BIND_HOST", "0.0.0.0"),
        port=parsed.port or 80, lifespan="off", log_level="warning"))
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            while not server.started:
                if task.done():
                    await task
                    raise RuntimeError("Browser runtime server exited before startup")
                await asyncio.sleep(0.05)
        yield get_browser_service()
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10)
        except asyncio.TimeoutError:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await get_browser_service().shutdown()
