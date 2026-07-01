"""WebSocket support over the ASGI path.

Primary tests drive ``app.asgi`` directly with a fake ASGI websocket
(scope/receive/send) — no server needed. A final test boots uvicorn and uses a
real websocket client if the `websockets` library is available."""
import asyncio
import json

import pytest

from ombott_ng import Ombott, WebSocketDisconnect


def make_app():
    app = Ombott()

    @app.websocket('/echo')
    async def echo(ws):
        await ws.accept()
        async for msg in ws:
            await ws.send_text('echo:' + msg)

    @app.websocket('/room/<name>')
    async def room(ws, name):
        await ws.accept()
        await ws.send_json({'room': name})
        data = await ws.receive_json()
        await ws.send_json({'got': data})
        await ws.close(4000)

    @app.websocket('/reject')
    async def reject(ws):
        # close without accepting (must still consume the connect)
        await ws._recv_event()  # connect
        await ws.close(4003)

    return app


class FakeWS:
    """An in-process ASGI websocket peer: queue of inbound events + sent log."""

    def __init__(self, inbound):
        self._in = asyncio.Queue()
        for ev in inbound:
            self._in.put_nowait(ev)
        self.sent = []

    async def receive(self):
        return await self._in.get()

    async def send(self, ev):
        self.sent.append(ev)


async def run_ws(app, path, inbound):
    scope = {'type': 'websocket', 'path': path, 'headers': [],
             'query_string': b'', 'subprotocols': []}
    peer = FakeWS(inbound)
    await app.asgi(scope, peer.receive, peer.send)
    return peer.sent


async def test_echo():
    sent = await run_ws(make_app(), '/echo', [
        {'type': 'websocket.connect'},
        {'type': 'websocket.receive', 'text': 'a'},
        {'type': 'websocket.receive', 'text': 'b'},
        {'type': 'websocket.disconnect', 'code': 1000},
    ])
    assert sent[0] == {'type': 'websocket.accept'}
    assert {'type': 'websocket.send', 'text': 'echo:a'} in sent
    assert {'type': 'websocket.send', 'text': 'echo:b'} in sent


async def test_url_param_and_json():
    sent = await run_ws(make_app(), '/room/lobby', [
        {'type': 'websocket.connect'},
        {'type': 'websocket.receive', 'text': json.dumps({'hi': 1})},
    ])
    assert sent[0] == {'type': 'websocket.accept'}
    assert json.loads(sent[1]['text']) == {'room': 'lobby'}
    assert json.loads(sent[2]['text']) == {'got': {'hi': 1}}
    assert sent[-1] == {'type': 'websocket.close', 'code': 4000}


async def test_explicit_close():
    sent = await run_ws(make_app(), '/reject', [{'type': 'websocket.connect'}])
    assert sent == [{'type': 'websocket.close', 'code': 4003}]


async def test_no_route_rejected():
    sent = await run_ws(make_app(), '/nope', [{'type': 'websocket.connect'}])
    assert sent == [{'type': 'websocket.close', 'code': 1000}]


async def test_uvicorn_websocket_roundtrip():
    websockets = pytest.importorskip("websockets")
    import threading
    import uvicorn

    app = make_app()
    config = uvicorn.Config(app.asgi, host='127.0.0.1', port=8124,
                            log_level='error', interface='asgi3', ws='auto')
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        uri = "ws://127.0.0.1:8124/echo"
        ws = None
        for _ in range(100):
            try:
                ws = await websockets.connect(uri)
                break
            except OSError:
                await asyncio.sleep(0.1)
        assert ws is not None
        await ws.send("hello")
        assert await ws.recv() == "echo:hello"
        await ws.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
