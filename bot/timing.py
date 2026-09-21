"""Structured duration logs; never records prompts, SQL text, parameters or report data."""
import functools
import hashlib
import json
import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

_trace=ContextVar('timing_trace',default=None)
_parent=ContextVar('timing_parent',default=None)
log=logging.getLogger('bot.timing')
# RQ children do not inherit the launcher logging configuration after exec.
if not log.handlers:
    handler=logging.StreamHandler()
    handler.setFormatter(logging.Formatter('[timing] %(message)s'))
    log.addHandler(handler)
log.setLevel(logging.INFO)
log.propagate=False

def event(stage, **fields):
    try:
        log.info(json.dumps({'ts':time.time(),'trace_id':_trace.get(),'stage':stage,**fields},ensure_ascii=False))
    except Exception:
        pass  # Observability must never break analysis.

@contextmanager
def span(stage, **fields):
    sid=uuid.uuid4().hex[:12];parent=_parent.get();token=_parent.set(sid)
    started=time.perf_counter();status='ok'
    event(stage,event='start',span_id=sid,parent_span_id=parent,**fields)
    try:
        yield
    except BaseException as exc:
        status='error';fields['error_type']=type(exc).__name__
        raise
    finally:
        event(stage,event='end',span_id=sid,parent_span_id=parent,status=status,
              elapsed_ms=round((time.perf_counter()-started)*1000,2),**fields)
        _parent.reset(token)

def timed(stage):
    def decorate(fn):
        @functools.wraps(fn)
        def call(*args,**kwargs):
            with span(stage):return fn(*args,**kwargs)
        return call
    return decorate

def job_timed(stage):
    def decorate(fn):
        @functools.wraps(fn)
        def call(payload,*args,**kwargs):
            job=None
            try:
                from rq import get_current_job
                job=get_current_job()
            except Exception:pass
            job_id=getattr(job,'id',None)
            trace_id=payload.get('analysis_job_id') or job_id or uuid.uuid4().hex
            token=_trace.set(trace_id);pt=_parent.set(None)
            try:
                enqueued=getattr(job,'enqueued_at',None)
                fields={'job_id':job_id}
                if enqueued is not None:fields['queue_wait_ms']=round(max(0,time.time()-enqueued.timestamp())*1000,2)
                with span(stage,**fields):return fn(payload,*args,**kwargs)
            finally:_parent.reset(pt);_trace.reset(token)
        return call
    return decorate

def sql_timed(fn):
    @functools.wraps(fn)
    def call(sql,params=None):
        fingerprint=hashlib.sha256(str(sql).encode()).hexdigest()[:16]
        with span('sql.'+fn.__name__,sql_id=fingerprint):return fn(sql,params)
    return call

def instrument_client(client):
    original=client.chat.completions.create
    @functools.wraps(original)
    def create(*args,**kwargs):
        with span('llm.chat_completion',model=str(kwargs.get('model','unknown')),stream=bool(kwargs.get('stream',False))):
            return original(*args,**kwargs)
    client.chat.completions.create=create
    return client
