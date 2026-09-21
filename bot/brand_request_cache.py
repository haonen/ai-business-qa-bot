"""Request-local mapping reuse; never a process-wide or conversation cache."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
from threading import RLock

_current = ContextVar('brand_request_cache', default=None)

class RequestCache:
    def __init__(self):
        self.values = {}
        self.lock = RLock()
        self.key_locks = {}

    def get_or_compute(self, key, compute):
        # Copied thread contexts share this lock so concurrent consumers of the
        # same mapping do not race to read/write the persistent cache.
        with self.lock:
            key_lock = self.key_locks.setdefault(key, RLock())
        # Different sources still resolve concurrently.
        with key_lock:
            if key in self.values:
                return deepcopy(self.values[key])
            value = compute()
            # Errors and ambiguity must be checked again, not memoized.
            if value.get('status') == 'ready':
                self.values[key] = deepcopy(value)
            return value

@contextmanager
def brand_request_cache(*, fresh=False):
    if not fresh and _current.get() is not None:
        yield
        return
    token = _current.set(RequestCache())
    try:
        yield
    finally:
        _current.reset(token)

def request_cached(fn=None, *, fresh=False):
    def decorate(func):
        @wraps(func)
        def run(*args, **kwargs):
            with brand_request_cache(fresh=fresh):
                return func(*args, **kwargs)
        return run
    return decorate(fn) if fn is not None else decorate
