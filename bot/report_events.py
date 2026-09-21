"""Request-local lifecycle notifications from formal Plan template execution."""
from contextlib import contextmanager
from contextvars import ContextVar
_listener=ContextVar('formal_report_listener',default=None)
@contextmanager
def listen(callback):
    token=_listener.set(callback)
    try:yield
    finally:_listener.reset(token)
def notify(event,platform,brand,period,result=None):
    callback=_listener.get()
    if callback:callback(event,platform,brand,period,result)
