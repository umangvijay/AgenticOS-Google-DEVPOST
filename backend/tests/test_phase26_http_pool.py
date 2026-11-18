"""An unrelated event loop must never retire another worker's active HTTP pool."""
import asyncio
import threading

from backend.services.http_pool import aclose_client, get_client


def test_concurrent_workers_have_independent_http_pool_lifetimes():
    async def exercise():
        first = await get_client()
        ready = threading.Event()
        released = threading.Event()

        async def other_loop():
            other = await get_client()
            assert other is not first
            ready.set()
            try:
                assert await asyncio.to_thread(released.wait, 5)
            finally:
                await aclose_client()
            assert other.is_closed

        peer = asyncio.create_task(asyncio.to_thread(asyncio.run, other_loop()))
        try:
            assert await asyncio.to_thread(ready.wait, 5)
            assert first is await get_client() and not first.is_closed
        finally:
            released.set()
            try:
                await peer
                assert first is await get_client() and not first.is_closed
            finally:
                await aclose_client()
        assert first.is_closed
        await aclose_client()

    asyncio.run(exercise())
