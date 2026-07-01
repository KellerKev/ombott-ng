"""Server-Sent Events (SSE) helper.

A handler returns ``sse(source)`` where ``source`` is an async iterable yielding
events (a ``str`` -> ``data:``; or a ``dict`` with any of ``data``/``event``/
``id``/``retry``/``comment``; or raw ``bytes``). The ASGI path streams the
formatted ``text/event-stream`` body chunk-by-chunk. SSE needs the async (ASGI)
server — e.g. ``-s uvicorn``.
"""
from __future__ import annotations


def format_event(data=None, *, event=None, id=None, retry=None, comment=None) -> bytes:
    lines = []
    if comment is not None:
        lines.append(": " + str(comment))
    if event is not None:
        lines.append("event: " + str(event))
    if id is not None:
        lines.append("id: " + str(id))
    if retry is not None:
        lines.append("retry: " + str(int(retry)))
    if data is not None:
        for line in str(data).split("\n"):
            lines.append("data: " + line)
    return ("\n".join(lines) + "\n\n").encode("utf-8")


async def _sse_body(source):
    async for ev in source:
        if isinstance(ev, (bytes, bytearray)):
            yield bytes(ev)
        elif isinstance(ev, dict):
            yield format_event(**ev)
        else:
            yield format_event(ev)


def sse(source, *, response=None):
    """Set ``text/event-stream`` headers on the response and return an async body
    that formats ``source`` (async iterable of str/dict/bytes events)."""
    if response is None:
        from . import response as response  # the global Response proxy
    # Write to the per-context header dict (shared across Response instances via
    # the ts_props ContextVar) rather than the per-instance HeaderDict, so the
    # headers are seen by whichever Response instance the ASGI path streams.
    headers = response._headers
    if headers is None:
        headers = {}
        response._headers = headers
    headers["Content-Type"] = "text/event-stream; charset=utf-8"
    headers["Cache-Control"] = "no-cache"
    headers["X-Accel-Buffering"] = "no"
    return _sse_body(source)
