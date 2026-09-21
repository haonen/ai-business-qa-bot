from bot.timing import timed, job_timed, sql_timed, span, event
"""Bind the actual template invocation, including Plan fanout and separate BET dates."""
from bot.brand_request_cache import request_cached
from functools import wraps
from inspect import signature
from bot.brand_query import enabled, _scope, QueryScope, use_query_scope

def scoped_template(platform, *, whole_brand=False):
    def decorate(fn):
        sig=signature(fn)
        @request_cached
        @wraps(fn)
        def run(*args,**kwargs):
            if not enabled():return fn(*args,**kwargs)
            bound=sig.bind(*args,**kwargs);parent=_scope.get()
            # A template invoked by a Plan uses that step's explicit brand/date/platform.
            category='TTL' if whole_brand else ((parent.category if parent else None) or 'TTL')
            with use_query_scope(QueryScope(bound.arguments['brand'],category,platform,str(bound.arguments['period']))):
                with span('template.'+getattr(fn, '__name__', 'template')):
                    return fn(*args,**kwargs)
        return run
    return decorate
