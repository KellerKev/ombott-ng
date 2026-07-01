"""Server-Sent Events over the ASGI streaming path."""
import asyncio

import httpx
import pytest

from ombott_ng import Ombott, sse


def make_app():
    app = Ombott()

    @app.get('/events')
    async def events():
        async def gen():
            for i in range(3):
                yield {'data': 'tick %d' % i, 'id': i, 'event': 'tick'}
                await asyncio.sleep(0)
        return sse(gen())

    @app.get('/plain')
    async def plain():
        async def gen():
            yield 'hello'
            yield 'world'
        return sse(gen())

    return app


async def test_sse_events():
    app = make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.asgi),
                                 base_url='http://t') as c:
        r = await c.get('/events')
        assert r.status_code == 200
        assert r.headers['content-type'].startswith('text/event-stream')
        body = r.text
        assert body.count('data: tick') == 3
        assert 'event: tick' in body and 'id: 0' in body
        assert body.endswith('\n\n')  # SSE frames end with a blank line


async def test_sse_plain_strings():
    app = make_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.asgi),
                                 base_url='http://t') as c:
        r = await c.get('/plain')
        assert 'data: hello\n\n' in r.text
        assert 'data: world\n\n' in r.text


async def test_sse_uvicorn_stream():
    import threading
    import uvicorn

    app = make_app()
    config = uvicorn.Config(app.asgi, host='127.0.0.1', port=8125,
                            log_level='error', interface='asgi3')
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        async with httpx.AsyncClient(base_url='http://127.0.0.1:8125') as c:
            for _ in range(100):  # wait for the threaded server
                try:
                    await c.get('/plain')
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.1)
            lines = []
            async with c.stream('GET', '/events') as resp:
                assert resp.status_code == 200
                assert resp.headers['content-type'].startswith('text/event-stream')
                async for line in resp.aiter_lines():
                    lines.append(line)
            text = '\n'.join(lines)
            assert text.count('data: tick') == 3
    finally:
        server.should_exit = True
        thread.join(timeout=5)
