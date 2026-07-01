
from ..common_helpers import ts_props, SimpleConfig
from ..mixable import Mixable

from .props_mixin import PropsMixin
from .body_mixin import BodyMixin
from .helpers import FormsDict, CookieDict


class RequestConfig(SimpleConfig):
    app_name_header = ''
    errors_map = {}
    max_body_size = None
    max_memfile_size = 100 * 1024
    allow_x_script_name = False


class BaseRequest:
    __slots__ = ('environ', '_env_get', '__listeners__', 'config', '_ts_props')

    _forms_factory = FormsDict
    _cookie_factory = CookieDict

    def __new__(cls, environ = None, *, config=None):
        self = super().__new__(cls)
        self.__listeners__ = {}
        self.on('env_changed', cls._on_env_changed)
        self.config = RequestConfig.get_from(config)
        return self

    def __init__(self, environ = None, *, config = None):
        # self.environ = some  - also does self._env_get = some.get
        self.environ = {} if environ is None else environ
        self.environ['ombott.request'] = self

    def setup(self, config):
        self.config = RequestConfig.get_from(config)

    def _raise(self, err, except_class = None):
        errors_map = self.config.errors_map
        for err_cls in (err.__class__, except_class):
            out_err = errors_map.get(err_cls)
            if out_err:
                err = out_err
                break
        raise err

    @staticmethod
    def _on_env_changed(request, key, v):
        todelete = ()
        if key == 'wsgi.input':
            todelete = ('forms', 'files', 'params', 'post', 'json', 'body')
        elif key == 'QUERY_STRING':
            todelete = ('query', 'params')
        elif key.startswith('HTTP_'):
            todelete = ('headers', 'cookies')
        env = request.environ
        [env.pop('ombott.request.' + key, None) for key in todelete]

    # --- async streaming request body (ASGI) --------------------------------
    async def stream(self, chunk_size=65536):
        """Yield the request body in chunks.

        On a streaming route (registered with ``stream=True``) the body is NOT
        buffered, so this pulls chunks directly from the ASGI transport as they
        arrive — suitable for large uploads or long-lived/agentic streams. On a
        normal route it yields the already-buffered body.
        """
        receive = self.environ.get('ombott.asgi.receive')
        if receive is None:
            inp = self.environ.get('wsgi.input')
            if inp is not None:
                try:
                    inp.seek(0)
                except Exception:
                    pass
                while True:
                    chunk = inp.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk
            return
        more = True
        while more:
            ev = await receive()
            etype = ev.get('type')
            if etype == 'http.request':
                data = ev.get('body') or b''
                if data:
                    yield data
                more = ev.get('more_body', False)
            elif etype == 'http.disconnect':
                break
            else:
                more = False

    async def body_async(self) -> bytes:
        """Read and return the full request body (async)."""
        chunks = []
        async for c in self.stream():
            chunks.append(c)
        return b''.join(chunks)

    async def json_async(self):
        """Parse the body as JSON (async), reading the stream if needed."""
        import json
        raw = await self.body_async()
        return json.loads(raw.decode('utf-8')) if raw else None

    def on(self, e, cb):
        if e not in self.__listeners__:
            self.__listeners__[e] = []
        self.__listeners__[e].append(cb)
        return lambda: self.__listeners__[e].remove(cb)

    def off(self, e, cb):
        self.__listeners__[e].remove(cb)

    def emit(self, e, *a, **kw):
        if e not in self.__listeners__:
            return
        [cb(self, *a, **kw) for cb in self.__listeners__[e]]

    def copy(self):
        """ Return a new :class:`Request` with a shallow :attr:`environ` copy.

        NOTE: __listeners__ are not copied

        """
        copy = self.__class__(self.environ.copy(), config = self.config)
        return copy

    def get(self, value, default=None):
        return self._env_get(value, default)

    def keys(self):
        return self.environ.keys()

    def __iter__(self):
        return iter(self.environ)

    def __len__(self):
        return len(self.environ)

    def __getitem__(self, key):
        return self.environ[key]

    def __setitem__(self, key, value):
        """ Change an environ value and clear all caches that depend on it. """

        if self._env_get('ombott.request.readonly'):
            raise KeyError('The environ dictionary is read-only.')

        env = self.environ
        if key in env and env[key] in [value]:  # `in` performs 2 OR-gluing tests (`is` OR `==`)
            return

        env[key] = value
        self.emit('env_changed', key, value)

    def __delitem__(self, key):
        self[key] = ""
        del self.environ[key]

    def __getattr__(self, name):
        ''' Search in self.environ for additional user defined attributes. '''
        if name in self.__slots__:
            return
        try:
            var = self.environ['ombott.request.ext.%s' % name]
            getter = getattr(var, '__get__', None)
            return getter(self) if getter else var
        except KeyError:
            raise AttributeError('Attribute %r not defined.' % name)

    def __setattr__(self, name, value):
        if name in self.__slots__:
            ret = object.__setattr__(self, name, value)
            if name == 'environ':
                object.__setattr__(self, '_env_get', value.get)
            return ret
        self.environ['ombott.request.ext.%s' % name] = value
        return value

    def __repr__(self):
        return '<%s: %s %s>' % (self.__class__.__name__, self.method, self.url)


@ts_props('environ', '_env_get', store_name = '_ts_props')
class Request(Mixable, BaseRequest, PropsMixin, BodyMixin):
    _as_mixins = [PropsMixin, BodyMixin]
