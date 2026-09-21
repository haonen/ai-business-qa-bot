"""Explicit rollout/injection for the unified dictionary; disabled by default."""
import os
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from bot.brand_lookup import BrandLookup, SqlBrandRepository
_override=ContextVar('brand_lookup_override',default=None)

@contextmanager
def use_brand_lookup(lookup):
    token=_override.set(lookup)
    try:yield
    finally:_override.reset(token)

@lru_cache(maxsize=1)
def _database_lookup():
    from sqlalchemy import create_engine
    from bot.db.connection import get_engine
    # Separate pool: newsletter business-query connections are READ ONLY.
    engine=create_engine(get_engine().url,pool_pre_ping=True,pool_size=1,max_overflow=1,
                         connect_args={'connect_timeout':10,'read_timeout':30})
    return BrandLookup(SqlBrandRepository(engine))

def get_brand_lookup():
    if _override.get() is not None:return _override.get()
    if os.environ.get('BRAND_LOOKUP_ENABLED','0')!='1':return None
    return _database_lookup()
