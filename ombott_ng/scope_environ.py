"""Build a WSGI ``environ`` from an ASGI HTTP ``scope`` (+ already-buffered body).

By synthesising a WSGI-style environ and putting the request body in a BytesIO at
``environ['wsgi.input']``, the entire existing request layer (``request_pkg`` —
headers, query, forms, json, multipart, cookies) works **unchanged** on the ASGI
path. Only the transport (how the body arrives and how the response leaves)
differs; the request/response *objects* and the router are shared with WSGI.
"""
from __future__ import annotations

import io
import sys


async def read_body(receive) -> bytes:
    """Drain the ASGI request body from ``receive()`` into bytes."""
    chunks = []
    more = True
    while more:
        event = await receive()
        etype = event.get("type")
        if etype == "http.request":
            body = event.get("body") or b""
            if body:
                chunks.append(body)
            more = event.get("more_body", False)
        elif etype == "http.disconnect":
            break
        else:
            more = False
    return b"".join(chunks)


def environ_from_scope(scope, body: bytes = None, receive=None) -> dict:
    """Map an ASGI ``http`` scope to a WSGI environ dict.

    If ``body`` is provided it is placed (buffered) at ``wsgi.input``. Otherwise
    an empty placeholder is used and ``receive`` is stashed so the body can be
    either buffered later (non-streaming routes) or read incrementally via
    ``request.stream()`` (streaming routes).
    """
    server = scope.get("server") or ("127.0.0.1", 80)
    client = scope.get("client") or ("", 0)
    qs = scope.get("query_string", b"") or b""
    if isinstance(qs, (bytes, bytearray)):
        qs = qs.decode("latin1")

    environ = {
        "REQUEST_METHOD": (scope.get("method") or "GET").upper(),
        "SCRIPT_NAME": scope.get("root_path", "") or "",
        "PATH_INFO": scope.get("path", "/") or "/",
        "QUERY_STRING": qs,
        "SERVER_NAME": server[0] or "127.0.0.1",
        "SERVER_PORT": str(server[1] or 80),
        "SERVER_PROTOCOL": "HTTP/%s" % scope.get("http_version", "1.1"),
        "REMOTE_ADDR": (client[0] or "") if client else "",
        "wsgi.version": (1, 0),
        "wsgi.url_scheme": scope.get("scheme", "http"),
        "wsgi.input": io.BytesIO(body if body is not None else b""),
        "wsgi.errors": sys.stderr,
        "wsgi.multithread": True,
        "wsgi.multiprocess": False,
        "wsgi.run_once": False,
        "ombott.asgi": True,
    }
    if receive is not None:
        environ["ombott.asgi.receive"] = receive

    for k, v in (scope.get("headers") or []):
        name = k.decode("latin1").upper().replace("-", "_")
        val = v.decode("latin1")
        if name == "CONTENT_TYPE":
            environ["CONTENT_TYPE"] = val
        elif name == "CONTENT_LENGTH":
            environ["CONTENT_LENGTH"] = val
        elif name == "TRANSFER_ENCODING":
            # the ASGI server already de-chunked the body; don't let the request
            # layer try to parse chunked encoding on an already-decoded body.
            continue
        else:
            key = "HTTP_" + name
            if key in environ:
                environ[key] += "," + val
            else:
                environ[key] = val

    # request_pkg reads the body via CONTENT_LENGTH + wsgi.input.read
    if body is not None and "CONTENT_LENGTH" not in environ:
        environ["CONTENT_LENGTH"] = str(len(body))
    return environ


def headerlist_to_asgi(headerlist):
    """Encode WSGI (name, value) str headers to ASGI (bytes, bytes) tuples."""
    return [
        (name.encode("latin1"), str(value).encode("latin1"))
        for name, value in headerlist
    ]
