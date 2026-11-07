"""In-process event bus: the library scanner and the job
workers publish small events, ``/api/stream`` streams them to browsers as
server-sent events so the page reacts in well under a second instead of polling.

Publishing never blocks a producer: each subscriber has a bounded queue and the
oldest event is dropped when it is full (the UI only needs the latest state).
"""

import json
import logging
import queue
import threading
import time

logger = logging.getLogger("wclips.events")


class EventBus:
    def __init__(self, maxsize: int = 200):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self.maxsize = maxsize
        self.published = 0

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=self.maxsize)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)

    @property
    def subscribers(self) -> int:
        return len(self._subs)

    def publish(self, kind: str, **data) -> None:
        evt = {"kind": kind, "t": time.time(), **data}
        self.published += 1
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(evt)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(evt)
                except (queue.Empty, queue.Full):
                    pass


bus = EventBus()


def sse_line(evt: dict) -> str:
    return f"event: {evt['kind']}\ndata: {json.dumps(evt)}\n\n"
