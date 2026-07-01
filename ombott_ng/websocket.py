"""Minimal ASGI WebSocket support for ombott.

A ``WebSocket`` wraps the ASGI ``(scope, receive, send)`` for a ``websocket``
scope and exposes ergonomic accept/receive/send/close helpers. Register
handlers with ``@app.websocket('/ws')``; the handler is
``async def handler(ws, **url_args)``.
"""
from __future__ import annotations

import json as _json


class WebSocketDisconnect(Exception):
    def __init__(self, code: int = 1000):
        self.code = code
        super().__init__("websocket disconnected (code=%s)" % code)


class WebSocketError(Exception):
    pass


class WebSocket:
    def __init__(self, scope, receive, send):
        self.scope = scope
        self._receive = receive
        self._send = send
        self.accepted = False
        self.closed = False
        self.client_closed = False

    # --- scope info ---------------------------------------------------------
    @property
    def path(self):
        return self.scope.get("path", "/")

    @property
    def query_string(self) -> bytes:
        return self.scope.get("query_string", b"") or b""

    @property
    def headers(self):
        return self.scope.get("headers") or []

    def header(self, name, default=None):
        key = name.lower().encode("latin1")
        for k, v in self.headers:
            if k.lower() == key:
                return v.decode("latin1")
        return default

    @property
    def subprotocols(self):
        return self.scope.get("subprotocols") or []

    # --- handshake ----------------------------------------------------------
    async def accept(self, subprotocol=None, headers=None):
        msg = await self._receive()
        if msg["type"] != "websocket.connect":
            raise WebSocketError("expected websocket.connect, got %r" % msg.get("type"))
        ev = {"type": "websocket.accept"}
        if subprotocol is not None:
            ev["subprotocol"] = subprotocol
        if headers is not None:
            ev["headers"] = headers
        await self._send(ev)
        self.accepted = True

    # --- receive ------------------------------------------------------------
    async def _recv_event(self):
        msg = await self._receive()
        if msg["type"] == "websocket.disconnect":
            self.client_closed = True
            self.closed = True
            raise WebSocketDisconnect(msg.get("code", 1000))
        return msg

    async def receive(self):
        """Return the next message payload (str for text, bytes for binary)."""
        msg = await self._recv_event()
        if msg.get("text") is not None:
            return msg["text"]
        return msg.get("bytes")

    async def receive_text(self) -> str:
        msg = await self._recv_event()
        if msg.get("text") is None:
            raise WebSocketError("expected text frame")
        return msg["text"]

    async def receive_bytes(self) -> bytes:
        msg = await self._recv_event()
        if msg.get("bytes") is None:
            raise WebSocketError("expected binary frame")
        return msg["bytes"]

    async def receive_json(self):
        return _json.loads(await self.receive_text())

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.receive()
        except WebSocketDisconnect:
            raise StopAsyncIteration

    # --- send ---------------------------------------------------------------
    async def send_text(self, data: str):
        await self._send({"type": "websocket.send", "text": data})

    async def send_bytes(self, data: bytes):
        await self._send({"type": "websocket.send", "bytes": data})

    async def send_json(self, obj):
        await self.send_text(_json.dumps(obj))

    async def send(self, data):
        if isinstance(data, (bytes, bytearray)):
            await self.send_bytes(bytes(data))
        else:
            await self.send_text(data)

    # --- close --------------------------------------------------------------
    async def close(self, code: int = 1000, reason: str = None):
        if self.closed:
            return
        ev = {"type": "websocket.close", "code": code}
        if reason is not None:
            ev["reason"] = reason
        await self._send(ev)
        self.closed = True
