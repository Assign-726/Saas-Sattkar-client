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
