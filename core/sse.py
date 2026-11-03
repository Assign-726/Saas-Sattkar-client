import json
import threading
from queue import Queue, Empty
from typing import Dict, List, Tuple

_registry_lock = threading.Lock()
_registry: Dict[str, List[Tuple[Queue, threading.Event]]] = {}


def subscribe(channel: str) -> Tuple[Queue, threading.Event]:
    """
    Subscribe to a channel (e.g., restaurant slug). Returns a (queue, closed_event).
    The consumer should read from the queue; when closed_event is set, stop streaming.
    """
    q: Queue = Queue()
    closed = threading.Event()
    with _registry_lock:
        _registry.setdefault(channel, []).append((q, closed))
    return q, closed


def unsubscribe(channel: str, q: Queue, closed: threading.Event) -> None:
    with _registry_lock:
        subs = _registry.get(channel)
        if not subs:
            return
        try:
            subs.remove((q, closed))
        except ValueError:
            pass
        if not subs:
            _registry.pop(channel, None)
    try:
        closed.set()
    except Exception:
        pass


def publish(channel: str, payload: dict) -> None:
    """Publish a message to all subscribers on a channel."""
    data = json.dumps(payload)
    with _registry_lock:
        subs = list(_registry.get(channel, []))
    for q, _closed in subs:
        try:
            q.put_nowait(data)
        except Exception:
            # If queue is full or any error, best-effort ignore
            pass


def sse_event(data: str, event: str = None) -> bytes:
    """Format data as an SSE event."""
    lines = []
    if event:
        lines.append(f"event: {event}")
    for line in data.splitlines() or [""]:
        lines.append(f"data: {line}")
    lines.append("")  # terminator blank line
    return ("\n".join(lines) + "\n").encode("utf-8")


def sse_comment(comment: str) -> bytes:
    return (f": {comment}\n\n").encode("utf-8")


# A stream holds its request open. On a server that runs one request per process (gunicorn's default
# "sync" worker) every open admin tab pins a whole worker, so a few tabs starve the site and gunicorn
# kills the stuck workers on its timeout. So streams run only on threaded servers (runserver, gunicorn
# gthread); elsewhere the endpoint answers 204, which tells the browser to stop reconnecting, and the
# pages fall back to their polling. SSE_STREAMS = 'auto' (default) | 'on' | 'off'.
STREAM_MAX_SECONDS = 55   # end each stream before gunicorn's / nginx's timeouts; the browser reconnects
STREAM_RETRY_MS = 3000
STREAM_KEEPALIVE_SECONDS = 5


def streams_allowed(request) -> bool:
    from django.conf import settings
    mode = str(getattr(settings, 'SSE_STREAMS', 'auto')).lower()
    if mode in ('off', 'false', '0', 'no'):
        return False
    if mode in ('on', 'true', '1', 'yes'):
        return True
    return bool(request.META.get('wsgi.multithread'))


def stream_response(request, channel: str, primary_event: str):
    """
    A Server-Sent Events response for `channel`: every payload goes out as `primary_event`, and again
    under its own `type` when that differs. Returns 204 when streams are not allowed (see above).
    """
    from django.http import HttpResponse, StreamingHttpResponse

    if not streams_allowed(request):
        resp = HttpResponse(status=204)
        resp['Cache-Control'] = 'no-store'
        return resp

    def event_stream():
        import time
        from django.db import connection
        # Nothing below touches the database: give the connection back instead of holding it open
        # for the life of the stream (one Postgres connection per open tab otherwise).
        try:
            connection.close()
        except Exception:
            pass
        q, closed = subscribe(channel)
        started = last_ping = time.monotonic()
        try:
            yield f"retry: {STREAM_RETRY_MS}\n".encode("utf-8") + sse_comment(f"stream opened for {channel}")
            while not closed.is_set():
                now = time.monotonic()
                if now - started > STREAM_MAX_SECONDS:
                    break
                # The server only notices a closed tab when a write fails, so ping often: a stream left
                # behind by a page change frees its worker thread within ~5 s instead of ~15 s.
                if now - last_ping > STREAM_KEEPALIVE_SECONDS:
                    last_ping = now
                    yield sse_comment('keepalive')
                try:
                    data = q.get(timeout=1.0)
                except Empty:
                    continue
                yield sse_event(data, event=primary_event)
                try:
                    etype = json.loads(data).get('type')
                    if etype and etype != primary_event:
                        yield sse_event(data, event=etype)
                except Exception:
                    pass
        finally:
            unsubscribe(channel, q, closed)

    response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'  # nginx: pass events through unbuffered
    return response
