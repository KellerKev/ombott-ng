import sys
import json
import functools
import contextvars
from inspect import iscoroutine
from traceback import format_exc
import itertools
from urllib.parse import urljoin

from .scope_environ import environ_from_scope, read_body, headerlist_to_asgi

from .common_helpers import (
    html_escape,
    tob,
    cached_property,
    WSGIFileWrapper,
    SimpleConfig,
)
from .router import HookTypes, RadiRouter
from .request_pkg import Request, errors as request_errors
from .response import Response, HTTPResponse, HTTPError
from . import server_adapters
from . import error_render

__version__ = "2.5"

HTTP_METHODS = 'DELETE GET HEAD OPTIONS PATCH POST PUT'.split()


# The ombott app currently handling a request in this (thread / async-task)
# context. It's what makes the module-level ``request``/``response`` (and
# ``redirect()``) resolve to the right instance when several apps run in one
# process -- e.g. a single async server multiplexing several isolated apps
# concurrently. ``None`` outside a request => fall back to the default app.
_current_app = contextvars.ContextVar('ombott_current_app', default=None)


class _CurrentProxy:
    """A stand-in for ``request``/``response`` that always forwards to the app
    currently serving in this context (or the default app outside a request).

    There is exactly one proxy per attribute, so any code that snapshots
    ``ombott.request`` / ``ombott.response`` at import time (websaw's ``globs``
    and ``BaseContext``, ``redirect()``, ...) keeps a stable reference that still
    tracks the live per-request object -- correct even under concurrency.
    """
    __slots__ = ('_attr',)

    def __init__(self, attr):
        object.__setattr__(self, '_attr', attr)

    def _t(self):
        app = _current_app.get()
        return getattr(app if app is not None else Globals.app, self._attr)

    def __getattr__(self, name):
        return getattr(self._t(), name)

    def __setattr__(self, name, value):
        setattr(self._t(), name, value)

    def __delattr__(self, name):
        delattr(self._t(), name)

    def __getitem__(self, k):
        return self._t()[k]

    def __setitem__(self, k, v):
        self._t()[k] = v

    def __delitem__(self, k):
        del self._t()[k]

    def __contains__(self, k):
        return k in self._t()

    def __iter__(self):
        return iter(self._t())

    def __len__(self):
        return len(self._t())

    def __repr__(self):
        return repr(self._t())


@SimpleConfig.keys_holder
class DefaultConfig(SimpleConfig):
    catchall = True
    debug = False
    domain_map = {}

    # request specific
    app_name_header = ''
    errors_map = {
        request_errors.RequestError: HTTPError(400, 'Bad request'),
        request_errors.BodySizeError: HTTPError(413, 'Request entity too large'),
        request_errors.BodyParsingError: HTTPError(400, 'Error while parsing chunked transfer body'),
        request_errors.JSONParsingError: HTTPError(400, 'Invalid json'),
    }
    max_body_size = None
    max_memfile_size = 100 * 1024
    allow_x_script_name = False


class _closeiter:
    ''' This only exists to be able to attach a .close method to iterators that
        do not support attribute assignment (most of itertools). '''

    def __init__(self, iterator, close=None):
        self.iterator = iterator
        self.close_callbacks = close if isinstance(close, (list, tuple)) else [close]

    def __iter__(self):
        return iter(self.iterator)

    def close(self):
        [cb() for cb in self.close_callbacks]


def run(app=None, server='wsgiref', host='127.0.0.1', port=8080,
        quiet=False, **kwargs):
    _stderr = sys.stderr.write
    try:
        app = app or default_app()
        if not callable(app):
            raise ValueError("Application is not callable: %r" % app)
        server_names = server_adapters.server_names
        if server in server_names:
            server = server_names.get(server)
        server = server(host=host, port=port, **kwargs)
        server.quiet = server.quiet or quiet
        # ASGI adapters serve the app's ASGI callable; WSGI adapters the app itself
        served = getattr(app, 'asgi', app) if getattr(server, 'is_asgi', False) else app
        if not server.quiet:
            _stderr("Ombott v%s server starting up (using %s)...\n" % (__version__, repr(server)))
            if not server.host.startswith('unix:/'):
                schema = 'https' if kwargs.get('certfile') else 'http'
                _stderr(f"Listening on {schema}://{server.host}:{server.port}/\n")
            else:
                _stderr(f"Listening on {server.host}\n")

            _stderr("Hit Ctrl-C to quit.\n\n")
        server.run(served)
    except KeyboardInterrupt:
        pass
    except (SystemExit, MemoryError):
        raise
    except:  # noqa
        raise


def with_method_shortcuts(methods):
    def injector(cls):
        for m in methods:
            setattr(cls, m.lower(), functools.partialmethod(cls.route, method=m))
        return cls
    return injector

###############################################################################
# Application Object ###########################################################
###############################################################################


@with_method_shortcuts(HTTP_METHODS)
class Ombott:
    __slots__ = ('config', 'router', 'request', 'response', '_route_hooks', 'error_handlers', '__dict__')

    def __init__(self, config=None):
        self.config = config = DefaultConfig(config)
        self.router = RadiRouter()
        self.request = Request(config=config)
        self.response = Response()
        self._route_hooks = {}
        self.error_handlers = {'404-hooks': {}}

    def setup(self, config=None):
        self.config = config = DefaultConfig(config)
        self.request.setup(config)

    def run(self, **kwargs):
        ''' Calls :func:`run` with the same parameters. '''
        run(self, **kwargs)

    def to_route(self, path, verb):
        if verb == 'HEAD':
            methods = [verb, 'GET', 'ANY']
        else:
            methods = [verb, 'ANY']
        end_point, error404_405 = self.router.resolve(path, methods)
        return (end_point, error404_405)

    def add_route(self, rule, method, handler, name=None, *, overwrite=False):
        return self.router.add(rule, method, handler, name, overwrite=overwrite)

    def remove_route(self, rule=None, *, route_pattern=None, name=None):
        self.router.remove(rule, route_pattern=route_pattern, name=name)

    def route(self, rule=None, method='GET', callback=None,
              *, name=None, overwrite=False, stream=False):
        def decorator(callback):
            if stream:
                try:
                    callback.__ombott_stream__ = True
                except (AttributeError, TypeError):
                    pass
            self.add_route(rule, method, callback, name, overwrite=overwrite)
            return callback
        return decorator(callback) if callback else decorator

    def websocket(self, rule=None, callback=None, *, name=None, overwrite=False):
        """Register an ASGI WebSocket handler: ``async def handler(ws, **args)``."""
        def decorator(callback):
            self.add_route(rule, 'WEBSOCKET', callback, name, overwrite=overwrite)
            return callback
        return decorator(callback) if callback else decorator

    # overwrites by @with_method_shortcuts()
    def delete(self, rule=None, callback=None, *, name=None, overwrite=False): pass
    def get(self, rule=None, callback=None, *, name=None, overwrite=False): pass
    def head(self, rule=None, callback=None, *, name=None, overwrite=False): pass
    def options(self, rule=None, callback=None, *, name=None, overwrite=False): pass
    def patch(self, rule=None, callback=None, *, name=None, overwrite=False): pass
    def post(self, rule=None, callback=None, *, name=None, overwrite=False): pass
    def put(self, rule=None, callback=None, *, name=None, overwrite=False): pass

    @property
    def routes(self):
        return self.router.routes

    __hook_names = ('before_request', 'after_request')
    __hook_reversed = {'after_request'}

    @cached_property
    def _hooks(self):
        return {name: [] for name in self.__hook_names}

    def add_hook(self, name, func):
        """Attach a callback to a hook.

        Three hooks are currently implemented:
            `before_request`
                Executed once before each request. The request context is
                available, but no routing has happened yet.
            `after_request`
                Executed once after each request regardless of its outcome.

        """
        if name in self.__hook_reversed:
            self._hooks[name].insert(0, func)
        else:
            self._hooks[name].append(func)

    def on(self, name, func=None):
        if not func:  # used as decorator
            def decorator(func):
                self.add_hook(name, func)
                return func
            return decorator
        else:
            self.add_hook(name, func)

    def remove_hook(self, name, func):
        if func in self._hooks[name]:
            self._hooks[name].remove(func)
            return True

    def emit(self, name, *args, **kwargs):
        [hook(*args, **kwargs) for hook in self._hooks[name][:]]

    def on_route(self, rule, func=None):
        if not func:  # used as decorator
            def decorator(func):
                self.router.add_hook(rule, func)
                return func
            return decorator
        else:
            self.router.add_hook(rule, func)

    def remove_route_hook(self, rule):
        self.router.remove_hook(rule)

    def error(self, code=500, rule=None):
        """ Decorator: Register an output handler for a HTTP error code"""
        code = int(code)

        def wrapper(handler):
            if code == 404 and rule:
                route_pattern = self.router.add_hook(
                    rule, handler, hook_type=HookTypes.PARTIAL
                )
                self.error_handlers['404-hooks'][route_pattern] = handler
            else:
                self.error_handlers[code] = handler
            return handler
        return wrapper

    def default_error_handler(self, res):
        if self.request.is_json_requested:
            ret = json.dumps(dict(
                body = res.body,
                exception = repr(res.exception),
                traceback = res.traceback
            ))
            self.response.headers['Content-Type'] = 'application/json'
        else:
            ret = error_render.render(res, self.request.url, self.config.debug)
        return ret

    @staticmethod
    def handler(app: 'Ombott', route, kwargs, route_hooks, error404_405):
        if error404_405:
            status, body, extra = error404_405
            if status == 405:
                raise HTTPError(status, body, Allow=extra)
            else:  # not found
                hooks_collected = extra['hooks']
                if hooks_collected:
                    route_pos, hooks = hooks_collected[-1]
                    partial_hook = hooks[HookTypes.PARTIAL]
                    if partial_hook:
                        hook_route = app.request.path[:1 + route_pos]
                        return partial_hook(hook_route, extra['param_values'])
                raise HTTPError(status, body)

        if route_hooks:
            path = app.request.path
            for route_pos, hooks in route_hooks:
                hook = hooks[HookTypes.SIMPLE]
                if hook:
                    hook(path[:1 + route_pos])
        return route(**kwargs)

    def _handle(self, environ):
        response = self.response
        request = self.request

        path = environ['ombott.raw_path'] = environ['PATH_INFO']
        try:
            path = path.encode('latin1').decode('utf8')
        except UnicodeError:
            return HTTPError(400, 'Invalid path string. Expected UTF-8')
        environ['PATH_INFO'] = path
        token = _current_app.set(self)
        try:  # init thread
            environ['ombott.app'] = self
            request.__init__(environ)
            response.__init__()
            try:  # routing
                self.emit('before_request')
                route, kwargs, route_hooks = (None, None, None)
                end_point, error404_405 = self.to_route(request.path, request.method)
                if end_point:
                    route, kwargs, route_hooks = end_point
                    environ['ombott.route'] = route
                    environ['route.url_args'] = kwargs
                    environ['route.hooks'] = route_hooks
                return self.handler(self, route, kwargs, route_hooks, error404_405)
            finally:
                self.emit('after_request')
        except HTTPResponse as resp:
            return resp
        except (KeyboardInterrupt, SystemExit, MemoryError):
            raise
        except Exception as err500:
            # raise
            stacktrace = format_exc()
            environ['wsgi.errors'].write(stacktrace)
            return HTTPError(500, "Internal Server Error", err500, stacktrace)
        finally:
            _current_app.reset(token)

    def _cast(self, out):
        """ Try to convert the parameter into something WSGI compatible and set
        correct HTTP headers when possible.
        Support: False, str, unicode, dict, HTTPResponse, HTTPError, file-like,
        iterable of strings and iterable of unicodes
        """

        response = self.response
        resp_headers = response.headers
        request = self.request
        loops_cnt = 0
        while True:   # <-------
            loops_cnt += 1
            if loops_cnt > 1000:
                out = HTTPError(500, 'too many iterations')
                out.apply(response)
                out = self.default_error_handler(out)

            # Empty output is done here
            if not out:
                resp_headers.setdefault('Content-Length', 0)
                return []

            if isinstance(out, str):
                out = out.encode(response.charset)

            # Byte Strings are just returned
            if isinstance(out, bytes):
                resp_headers.setdefault('Content-Length', len(out))
                return [out]

            if isinstance(out, HTTPError):
                out.apply(response)
                out = self.error_handlers.get(
                    out.status_code,
                    self.default_error_handler
                )(out); continue                         # -----------------^

            if isinstance(out, HTTPResponse):
                out.apply(response)
                out = out.body; continue                 # -----------------^

            # File-like objects.
            if hasattr(out, 'read'):
                if 'wsgi.file_wrapper' in request.environ:
                    return request.environ['wsgi.file_wrapper'](out)
                elif hasattr(out, 'close') or not hasattr(out, '__iter__'):
                    return WSGIFileWrapper(out)

            # Handle Iterables. We peek into them to detect their inner type.
            try:
                iout = iter(out)
                first = next(iout)
                while not first:
                    first = next(iout)
            except StopIteration:
                out = ''; continue                               # -----------------^
            except HTTPResponse as rs:
                first = rs
            except (KeyboardInterrupt, SystemExit, MemoryError):
                raise
            except Exception as err500:
                # if not self.catchall: raise
                first = HTTPError(500, 'Unhandled exception', err500, format_exc())

            # These are the inner types allowed in iterator or generator objects.
            if isinstance(first, HTTPResponse):
                out = first; continue                            # -----------------^
            elif isinstance(first, bytes):
                new_iter = itertools.chain([first], iout)
            elif isinstance(first, str):
                new_iter = (it.encode(response.charset) for it in itertools.chain([first], iout))
            else:
                out = HTTPError(500, f'Unsupported response type: {type(first)}')
                continue                                         # -----------------^
            close = getattr(out, 'close', None)
            if close:
                new_iter = _closeiter(new_iter, close)
            return new_iter

    def wsgi(self, environ, start_response):
        config: DefaultConfig = self.config
        response = self.response

        domain_map = config.domain_map
        if domain_map:
            app_name = domain_map(environ.get('HTTP_X_FORWARDED_HOST') or environ.get('HTTP_HOST'))
            if app_name:
                environ[config.app_name_header] = '/' + app_name
                environ["PATH_INFO"] = '/' + app_name + environ["PATH_INFO"]

        try:
            out = self._cast(self._handle(environ))
            # rfc2616 section 4.3
            if (
                response._status_code in {100, 101, 204, 304}
                or environ['REQUEST_METHOD'] == 'HEAD'
            ):
                close = getattr(out, 'close', None)
                if close:
                    close()
                out = []
            start_response(response._status_line, response.headerlist)
            return out
        except (KeyboardInterrupt, SystemExit, MemoryError):
            raise
        except Exception as _e:
            if not self.config.catchall:
                raise

            err = '<h1>Critical error while processing request: %s</h1>' \
                  % html_escape(environ.get('PATH_INFO', '/'))
            if self.config.debug:
                err += (
                    '<h2>Error:</h2>\n<pre>\n%s\n</pre>\n'
                    '<h2>Traceback:</h2>\n<pre>\n%s\n</pre>\n'
                    % (html_escape(repr(_e)), html_escape(format_exc()))
                )
            environ['wsgi.errors'].write(err)
            headers = [('Content-Type', 'text/html; charset=UTF-8')]
            start_response('500 INTERNAL SERVER ERROR', headers, sys.exc_info())
            return [tob(err)]

    def __call__(self, environ, start_response):
        return self.wsgi(environ, start_response)

    # =====================================================================
    # ASGI (async) entrypoint — shares the router, request/response objects
    # and _cast() with the WSGI path above; wsgi()/_handle() are untouched.
    # =====================================================================
    async def _ahandle(self, environ):
        """Async twin of _handle: same routing/hooks, but awaits coroutine handlers."""
        response = self.response
        request = self.request

        path = environ['ombott.raw_path'] = environ['PATH_INFO']
        try:
            path = path.encode('latin1').decode('utf8')
        except UnicodeError:
            return HTTPError(400, 'Invalid path string. Expected UTF-8')
        environ['PATH_INFO'] = path
        token = _current_app.set(self)
        try:
            environ['ombott.app'] = self
            request.__init__(environ)
            response.__init__()
            try:
                self.emit('before_request')
                route, kwargs, route_hooks = (None, None, None)
                end_point, error404_405 = self.to_route(request.path, request.method)
                if end_point:
                    route, kwargs, route_hooks = end_point
                    environ['ombott.route'] = route
                    environ['route.url_args'] = kwargs
                    environ['route.hooks'] = route_hooks
                # ASGI: buffer the body now (so sync .json/.POST work), UNLESS the
                # matched route opted into streaming (then request.stream() reads
                # it incrementally from the stashed `receive`).
                receive = environ.get('ombott.asgi.receive')
                if receive is not None:
                    handler_fn = getattr(route, 'handler', route)
                    streaming = (
                        getattr(handler_fn, '__ombott_stream__', False)
                        or getattr(route, '__ombott_stream__', False)
                    )
                    if not streaming:
                        from io import BytesIO
                        body = await read_body(receive)
                        environ['wsgi.input'] = BytesIO(body)
                        if not environ.get('CONTENT_LENGTH'):
                            environ['CONTENT_LENGTH'] = str(len(body))
                        environ.pop('ombott.asgi.receive', None)
                out = self.handler(self, route, kwargs, route_hooks, error404_405)
                if iscoroutine(out):
                    out = await out
                return out
            finally:
                self.emit('after_request')
        except HTTPResponse as resp:
            return resp
        except (KeyboardInterrupt, SystemExit, MemoryError):
            raise
        except Exception as err500:
            stacktrace = format_exc()
            environ['wsgi.errors'].write(stacktrace)
            return HTTPError(500, "Internal Server Error", err500, stacktrace)
        finally:
            _current_app.reset(token)

    async def asgi(self, scope, receive, send):
        stype = scope['type']
        if stype == 'lifespan':
            return await self._asgi_lifespan(receive, send)
        if stype == 'websocket':
            return await self._asgi_websocket(scope, receive, send)
        if stype != 'http':
            await send({'type': 'http.response.start', 'status': 404, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'Not Found'})
            return

        config: DefaultConfig = self.config
        response = self.response
        # body is NOT buffered up front: _ahandle buffers it after routing for
        # normal routes, or leaves it for request.stream() on streaming routes.
        environ = environ_from_scope(scope, receive=receive)

        domain_map = config.domain_map
        if domain_map:
            app_name = domain_map(environ.get('HTTP_X_FORWARDED_HOST') or environ.get('HTTP_HOST'))
            if app_name:
                environ[config.app_name_header] = '/' + app_name
                environ["PATH_INFO"] = '/' + app_name + environ["PATH_INFO"]

        try:
            raw = await self._ahandle(environ)
            # async-iterable output (e.g. SSE) is streamed chunk-by-chunk
            if hasattr(raw, '__aiter__') and not isinstance(raw, (bytes, str, dict)):
                await self._asgi_stream(send, response, raw)
                return
            out = self._cast(raw)
            # rfc2616 section 4.3
            if (
                response._status_code in {100, 101, 204, 304}
                or environ['REQUEST_METHOD'] == 'HEAD'
            ):
                close = getattr(out, 'close', None)
                if close:
                    close()
                out = []
            await self._asgi_emit(send, response._status_code, response.headerlist, out)
        except (KeyboardInterrupt, SystemExit, MemoryError):
            raise
        except Exception as _e:
            if not self.config.catchall:
                raise
            err = '<h1>Critical error while processing request: %s</h1>' \
                  % html_escape(environ.get('PATH_INFO', '/'))
            if self.config.debug:
                err += (
                    '<h2>Error:</h2>\n<pre>\n%s\n</pre>\n'
                    '<h2>Traceback:</h2>\n<pre>\n%s\n</pre>\n'
                    % (html_escape(repr(_e)), html_escape(format_exc()))
                )
            await send({
                'type': 'http.response.start', 'status': 500,
                'headers': [(b'content-type', b'text/html; charset=UTF-8')],
            })
            await send({'type': 'http.response.body', 'body': tob(err)})

    @staticmethod
    async def _asgi_emit(send, status_code, headerlist, body_iter):
        await send({
            'type': 'http.response.start',
            'status': int(status_code),
            'headers': headerlist_to_asgi(headerlist),
        })
        try:
            for chunk in body_iter:
                if chunk:
                    await send({
                        'type': 'http.response.body',
                        'body': bytes(chunk),
                        'more_body': True,
                    })
        finally:
            close = getattr(body_iter, 'close', None)
            if close:
                close()
        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

    async def _asgi_websocket(self, scope, receive, send):
        from .websocket import WebSocket, WebSocketDisconnect

        path = scope.get('path', '/') or '/'
        try:
            path = path.encode('latin1').decode('utf8')
        except UnicodeError:
            pass
        end_point, _err = self.to_route(path, 'WEBSOCKET')
        if not end_point:
            # no handler -> reject the handshake
            await send({'type': 'websocket.close', 'code': 1000})
            return
        route, kwargs, _route_hooks = end_point
        ws = WebSocket(scope, receive, send)
        try:
            await route(ws, **kwargs)
        except WebSocketDisconnect:
            pass
        except (KeyboardInterrupt, SystemExit, MemoryError):
            raise
        except Exception:
            if not self.config.catchall:
                raise
            sys.stderr.write(format_exc())
        finally:
            if not ws.closed:
                try:
                    await ws.close(1011)
                except Exception:
                    pass

    async def _asgi_stream(self, send, response, agen):
        """Stream an async-iterable response body (SSE / chunked streaming)."""
        await send({
            'type': 'http.response.start',
            'status': int(response._status_code),
            'headers': headerlist_to_asgi(response.headerlist),
        })
        charset = response.charset
        try:
            async for chunk in agen:
                if not chunk:
                    continue
                if isinstance(chunk, str):
                    chunk = chunk.encode(charset)
                await send({
                    'type': 'http.response.body',
                    'body': bytes(chunk),
                    'more_body': True,
                })
        finally:
            aclose = getattr(agen, 'aclose', None)
            if aclose is not None:
                try:
                    await aclose()
                except Exception:
                    pass
        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

    @staticmethod
    async def _asgi_lifespan(receive, send):
        while True:
            msg = await receive()
            if msg['type'] == 'lifespan.startup':
                await send({'type': 'lifespan.startup.complete'})
            elif msg['type'] == 'lifespan.shutdown':
                await send({'type': 'lifespan.shutdown.complete'})
                return


###############################################################################
# Application Helper ###########################################################
###############################################################################


def abort(code=500, text='Unknown Error'):
    """ Aborts execution and causes a HTTP error. """
    raise HTTPError(code, text)


def redirect(location, code=None):
    """ Aborts execution and causes a 303 or 302 redirect, depending on
        the HTTP protocol version.

    """
    response = Globals.response
    request = Globals.request
    url = location
    if not code:
        code = 303 if request.get('SERVER_PROTOCOL') == "HTTP/1.1" else 302
    res = response.copy(cls=HTTPResponse)
    res.status = code
    res.body = ""
    res.headers['Location'] = urljoin(request.url, url)
    raise res


class Globals:
    app = Ombott()
    route = app.route
    on_route = app.on_route
    # Context-local: resolve to whichever app is serving this request (see
    # _current_app / _CurrentProxy), so several apps can share one process.
    request = _CurrentProxy('request')
    response = _CurrentProxy('response')
    error = app.error


def default_app():
    return Globals.app
