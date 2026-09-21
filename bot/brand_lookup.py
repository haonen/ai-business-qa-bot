"""Unified mapping/cache path. Opt-in until the new tables are installed.

The SQL repository uses the two tables in brand-mapping-design/schema.sql.
No dependency on legacy dictionaries, source caches or media alias JSON.
"""
import hashlib
import json
from datetime import datetime, timedelta
from contextlib import nullcontext
from sqlalchemy import text
from sqlalchemy.engine import Connection
from bot.brand_mapping import BrandMapping, normalize


class SqlBrandRepository:
    def __init__(self, engine):
        self.engine = engine

    def _connect(self):
        return nullcontext(self.engine) if isinstance(self.engine, Connection) else self.engine.connect()

    def _transaction(self):
        if isinstance(self.engine, Connection):
            # Session-bound repositories support MySQL temporary-table trials.
            # The caller owns transaction completion and connection lifetime.
            return nullcontext(self.engine)
        return self.engine.begin()

    def mapping_rows(self):
        with self._connect() as conn:
            rows = [dict(r) for r in conn.execute(text(
                'SELECT * FROM ai_bot_brand_mapping')).mappings()]
        for row in rows:
            for field in ('scope_rule', 'evidence'):
                if isinstance(row.get(field), str):
                    row[field] = json.loads(row[field])
        return rows

    def get_cache(self, key):
        with self._connect() as conn:
            row = conn.execute(text(
                'SELECT * FROM ai_bot_brand_lookup_cache WHERE cache_key=:key'),
                {'key': key}).mappings().first()
        return dict(row) if row else None

    def put_cache(self, row):
        # One transactional upsert; unique-key contention must not affect resolution.
        from sqlalchemy.dialects.mysql import insert as mysql_insert
        from sqlalchemy import MetaData, Table
        with self._transaction() as conn:
            table = Table('ai_bot_brand_lookup_cache', MetaData(), autoload_with=conn)
            if self.engine.dialect.name == 'mysql':
                statement = mysql_insert(table).values(**row)
                conn.execute(statement.on_duplicate_key_update(
                    **{k: statement.inserted[k] for k in row if k != 'cache_key'}))
            else:
                # Used by local SQLite integration tests, not a MySQL compatibility claim.
                conn.execute(table.delete().where(table.c.cache_key == row['cache_key']))
                conn.execute(table.insert().values(**row))


class BrandLookup:
    def __init__(self, repository, source_availability=None, now=None):
        self.repository = repository
        self.source_availability = source_availability
        self.now = now or datetime.utcnow

    def plan(self, brand, category, table, field, context=None):
        from bot.brand_request_cache import _current
        cache = _current.get()
        if cache is None:
            return self._plan_uncached(brand, category, table, field, context)
        context_key = None if context is None else (
            context.brand_id, context.category, context.mapping_version)
        # Exact inputs and lookup instance separate sources, categories and
        # injected repositories. Do not normalize away potentially meaningful names.
        key = (self, brand, category, table, field, context_key)
        return cache.get_or_compute(key, lambda: self._plan_uncached(
            brand, category, table, field, context))

    def _plan_uncached(self, brand, category, table, field, context=None):
        mapping = BrandMapping(self.repository.mapping_rows(), self.source_availability)
        resolved = mapping.resolve(brand, category, context)
        if resolved['status'] != 'resolved':
            return resolved
        context = resolved['context']
        authoritative = mapping.source_plan(context, table, field)
        if authoritative['status'] != 'ready':
            return authoritative
        key = mapping.cache_key(brand or context.brand_id, context, table, field)
        context_hash = hashlib.sha256(json.dumps(
            [context.brand_id, context.category, table, field],
            ensure_ascii=False).encode()).hexdigest()
        cache_state = 'miss'
        try:
            cached = self.repository.get_cache(key)
            if cached:
                expiry = cached['expires_at']
                if isinstance(expiry, str):
                    expiry = datetime.fromisoformat(expiry)
                ids = cached['resolved_mapping_ids']
                if isinstance(ids, str):
                    ids = json.loads(ids)
                valid = (expiry > self.now() and
                         cached['brand_id'] == context.brand_id and
                         cached['scope_key'] == context.category and
                         cached['mapping_version'] == mapping.version and
                         cached['context_hash'] == context_hash and
                         sorted(ids) == authoritative['mapping_ids'])
                cache_state = 'hit' if valid else 'rejected'
        except Exception:
            cache_state = 'unavailable'
        if cache_state != 'hit':
            try:
                self.repository.put_cache({
                    'cache_key': key, 'normalized_input': normalize(brand or context.brand_id),
                    'brand_id': context.brand_id, 'scope_key': context.category,
                    'mapping_version': mapping.version, 'context_hash': context_hash,
                    'resolved_mapping_ids': authoritative['mapping_ids'],
                    'expires_at': self.now() + timedelta(hours=24)})
            except Exception:
                cache_state = 'write_failed'
        # Always derive source values from current verified rows, never cache strings.
        return dict(authoritative, context=context, cache_state=cache_state)
