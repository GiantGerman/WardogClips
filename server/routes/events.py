"""Server-sent events: library scans, recording updates, job progress. The page reacts
in well under a second instead of polling.

The stream lives on /api/stream, not /api/events: that path belongs to the detect router's
JSON event list, which would shadow the stream and leave the page's EventSource unconnected."""

import time
from queue import Empty

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from core.events import sse_line


def build(ctx) -> APIRouter:
    r = APIRouter()

    @r.get("/api/stream")
    async def events(request: Request):
        q = ctx.bus.subscribe()

        async def gen():
            last = time.time()
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        evt = await run_in_threadpool(q.get, True, 1.0)
                    except Empty:
                        evt = None
                    if evt is not None:
                        yield sse_line(evt)
                    elif time.time() - last > 15:
                        yield ": ping\n\n"
                        last = time.time()
                    if await request.is_disconnected():
                        return
            finally:
                ctx.bus.unsubscribe(q)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    return r
