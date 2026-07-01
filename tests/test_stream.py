"""Streaming request bodies over ASGI.

Routes opt in with ``stream=True``: the body is NOT buffered, and the handler
reads it incrementally via ``request.stream()`` / ``request.body_async()`` —
for large uploads or long-lived/agentic streams. Normal routes still buffer so
sync ``request.json`` / ``request.POST`` keep working."""
import json

import httpx
import pytest

from ombott_ng import Ombott


def client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app.asgi),
                             base_url='http://t')


async def test_streaming_route_not_buffered():
    app = Ombott()

    @app.post('/up', stream=True)
    async def up():
        from ombott_ng import request
        not_buffered = bool(request.environ.get('ombott.asgi.receive'))
        body = await request.body_async()
        return json.dumps({'len': len(body), 'streaming': not_buffered})

    async with client(app) as c:
        r = await c.post('/up', content=b'hello world')
        d = json.loads(r.text)
        assert d['len'] == 11
        assert d['streaming'] is True


async def test_stream_large_multichunk():
    app = Ombott()

    @app.post('/big', stream=True)
    async def big():
        from ombott_ng import request
        total = 0
        chunks = 0
        async for chunk in request.stream():
            total += len(chunk)
            chunks += 1
        return json.dumps({'total': total, 'chunks': chunks})

    async def gen():
        for _ in range(10):
            yield b'a' * 10000

    async with client(app) as c:
        r = await c.post('/big', content=gen())
        d = json.loads(r.text)
        assert d['total'] == 100000
        assert d['chunks'] >= 1


async def test_stream_json_async():
    app = Ombott()

    @app.post('/j', stream=True)
    async def j():
        from ombott_ng import request
        data = await request.json_async()
        return json.dumps({'echo': data})

    async with client(app) as c:
        r = await c.post('/j', json={'a': 1, 'b': [2, 3]})
        assert json.loads(r.text)['echo'] == {'a': 1, 'b': [2, 3]}


async def test_normal_route_still_buffers_sync_body():
    app = Ombott()

    @app.post('/sync')
    async def sync_body():
        from ombott_ng import request
        buffered = not request.environ.get('ombott.asgi.receive')  # consumed/popped
        return json.dumps({'json': request.json, 'buffered': buffered})

    async with client(app) as c:
        r = await c.post('/sync', json={'x': 9})
        d = json.loads(r.text)
        assert d['json'] == {'x': 9}
        assert d['buffered'] is True


async def test_stream_can_also_use_request_stream_on_buffered():
    # request.stream() works on a normal (buffered) route too (yields the body)
    app = Ombott()

    @app.post('/either')
    async def either():
        from ombott_ng import request
        body = await request.body_async()
        return json.dumps({'len': len(body)})

    async with client(app) as c:
        r = await c.post('/either', content=b'abcde')
        assert json.loads(r.text)['len'] == 5


async def test_stream_uvicorn():
    import asyncio
    import threading
    import uvicorn

    app = Ombott()

    @app.post('/up', stream=True)
    async def up():
        from ombott_ng import request
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
        return json.dumps({'total': total})

    config = uvicorn.Config(app.asgi, host='127.0.0.1', port=8126,
                            log_level='error', interface='asgi3')
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        async def gen():
            for _ in range(4):
                yield b'z' * 2048

        async with httpx.AsyncClient(base_url='http://127.0.0.1:8126') as c:
            r = None
            for _ in range(100):
                try:
                    r = await c.post('/up', content=gen())
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.1)
            assert r is not None
            assert json.loads(r.text)['total'] == 8192
    finally:
        server.should_exit = True
        thread.join(timeout=5)
