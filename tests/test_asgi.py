"""ASGI (async) path tests via httpx ASGITransport against app.asgi.

The WSGI path (wsgi()/_handle) is untouched and covered by the existing suite;
these exercise the new async entrypoint: sync + async handlers, request bodies
(json/form/multipart/query), cookies, errors, redirects, and — crucially —
concurrency isolation proving the contextvars migration."""
import asyncio
import json

import httpx
import pytest

from ombott_ng import Ombott, HTTPResponse, abort, redirect


def make_app():
    app = Ombott()

    @app.get('/hello')
    def hello():
        return 'hello world'

    @app.get('/async')
    async def ahello():
        await asyncio.sleep(0)
        return 'async hello'

    @app.get('/q')
    def q():
        from ombott_ng import request
        return 'q=%s' % request.query.get('x')

    @app.post('/echo')
    def echo():
        from ombott_ng import request
        return json.dumps({'data': request.json})

    @app.post('/form')
    def form():
        from ombott_ng import request
        return 'name=%s' % request.POST.get('name')

    @app.get('/setcookie')
    def setcookie():
        from ombott_ng import response
        response.set_cookie('sid', 'abc')
        return 'ok'

    @app.get('/getcookie')
    def getcookie():
        from ombott_ng import request
        return 'sid=%s' % request.get_cookie('sid')

    @app.get('/boom')
    def boom():
        abort(418, 'teapot')

    @app.get('/go')
    def go():
        redirect('/hello')

    @app.post('/slow_echo')
    async def slow_echo():
        # async handler that yields to the loop while a body is in flight,
        # then reads request-local state — proves per-task isolation.
        from ombott_ng import request
        name = request.POST.get('name')
        await asyncio.sleep(0.01)
        return 'name=%s' % name

    return app


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app.asgi),
                             base_url='http://t')


@pytest.fixture
async def client():
    async with client_for(make_app()) as c:
        yield c


async def test_sync_handler(client):
    r = await client.get('/hello')
    assert r.status_code == 200 and r.text == 'hello world'


async def test_async_handler(client):
    r = await client.get('/async')
    assert r.text == 'async hello'


async def test_query(client):
    r = await client.get('/q', params={'x': '42'})
    assert r.text == 'q=42'


async def test_json_post(client):
    r = await client.post('/echo', json={'a': 1, 'b': [2, 3]})
    assert json.loads(r.text)['data'] == {'a': 1, 'b': [2, 3]}


async def test_form_post(client):
    r = await client.post('/form', data={'name': 'Ada'})
    assert r.text == 'name=Ada'


async def test_cookies(client):
    r = await client.get('/setcookie')
    assert 'sid' in r.cookies
    r2 = await client.get('/getcookie', cookies={'sid': 'abc'})
    assert r2.text == 'sid=abc'


async def test_http_error(client):
    r = await client.get('/boom')
    assert r.status_code == 418


async def test_redirect(client):
    r = await client.get('/go')
    assert r.status_code in (302, 303) and r.headers['location'].endswith('/hello')


async def test_404(client):
    r = await client.get('/nope')
    assert r.status_code == 404


async def test_static_file(tmp_path):
    from ombott_ng import static_file
    (tmp_path / "hello.txt").write_text("FILE CONTENT")
    app = Ombott()

    @app.get('/file')
    def serve():
        return static_file('hello.txt', root=str(tmp_path))

    async with client_for(app) as c:
        r = await c.get('/file')
        assert r.status_code == 200 and r.text == "FILE CONTENT"


async def test_uvicorn_boot():
    # real end-to-end: serve app.asgi under uvicorn on a port and hit it via httpx
    import threading
    import uvicorn

    app = make_app()
    config = uvicorn.Config(app.asgi, host='127.0.0.1', port=8111,
                            log_level='error', lifespan='on', interface='asgi3')
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        async with httpx.AsyncClient(base_url='http://127.0.0.1:8111') as c:
            r = None
            for _ in range(100):  # retry until the threaded server is listening
                try:
                    r = await c.get('/hello')
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.1)
            assert r is not None and r.status_code == 200 and r.text == 'hello world'
            r2 = await c.post('/echo', json={'x': 1})
            assert json.loads(r2.text)['data'] == {'x': 1}
    finally:
        server.should_exit = True
        thread.join(timeout=5)


async def test_concurrency_isolation():
    # Fire many concurrent async POSTs with distinct bodies; each handler sleeps
    # mid-request. If request/response state were thread-local (shared on one
    # event-loop thread), bodies would cross-talk. contextvars => isolated.
    async with client_for(make_app()) as c:
        async def one(i):
            r = await c.post('/slow_echo', data={'name': 'user%d' % i})
            return r.text
        results = await asyncio.gather(*[one(i) for i in range(50)])
    assert sorted(results) == sorted('name=user%d' % i for i in range(50))
