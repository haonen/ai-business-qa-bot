from bot.timing import timed, job_timed, sql_timed, span, event
"""Table-specific authoritative brand predicates. Never truncate multiple names."""
import os
from dataclasses import dataclass
from contextlib import contextmanager
from contextvars import ContextVar
import re
from bot.brand_lookup_runtime import get_brand_lookup

@dataclass(frozen=True)
class QueryScope:
    brand: str
    category: str
    platform: str
    period: str

_scope = ContextVar('brand_query_scope', default=None)

@contextmanager
def use_query_scope(scope):
    token=_scope.set(scope)
    try:yield
    finally:_scope.reset(token)

class BrandQueryError(ValueError):
    def __init__(self,status):
        self.status=status
        super().__init__('品牌查询范围或映射不可用：'+status)


def enabled():
    return os.environ.get('BRAND_QUERY_GATE_ENABLED','0') == '1'


@timed('brand.resolve_source')
def brand_predicate(brand, table, field, *, category=None, prefix='source_brand'):
    for identifier in (table,field,prefix):
        if not re.fullmatch(r'[\w]+', identifier):raise BrandQueryError('invalid_identifier')
    scope=_scope.get()
    lookup=get_brand_lookup()
    if lookup is None:raise BrandQueryError('dictionary_disabled')
    # BET is whole-brand evidence, independent of the selected EC category.
    media_total = table in {'top_brands_total_ec','ai_bot_media_topline_investment','ai_bot_media_search_index','ai_bot_media_ksi_performance'}
    category = 'TTL' if media_total else (category or (scope.category if scope else None))
    if not media_total and scope and scope.category is not None and category != scope.category:
        raise BrandQueryError('category_scope_mismatch')
    result=lookup.plan(brand,category,table,field)
    if result.get('status')!='ready':raise BrandQueryError(result.get('status','unavailable'))
    if scope:
        locked=lookup.plan(scope.brand,category if media_total else (scope.category or category),table,field)
        if locked.get('status')!='ready' or locked['brand_id']!=result['brand_id']:
            raise BrandQueryError('brand_scope_mismatch')
    values=result['values']
    if not values:raise BrandQueryError('empty_mapping')
    params={f'{prefix}_{i}':value for i,value in enumerate(values)}
    predicate=f'`{field}` IN ('+','.join(':'+k for k in params)+')'
    platforms={'ai_bot_tmall_product_link':'TM','ai_bot_dy_product_link':'DY',
        'tmall_store_ranking_day_jiashicang':'TM','dy_store_ranking_BFSS_day_jiashicang':'DY',
        'jd_store_ranking_selfrun_day_jiashicang':'JD'}
    stores=set(platforms)-{'ai_bot_tmall_product_link','ai_bot_dy_product_link'}
    stores.add('three_platform_store_rank_monthly')
    if scope and table in platforms and scope.platform not in (platforms[table], 'TTL'):
        raise BrandQueryError('platform_scope_mismatch')
    if table in stores:
        from bot.tools.market_common import store_rank_business_category_sql
        labels={'TTL':'TOTAL BEAUTY','SKIN':'FEMALE SKINCARE','MEX':'MALE SKINCARE','MAKEUP':'MAKEUP','HAIR':'HAIR'}
        from bot.store_report_scope import filter_sql
        predicate=f'({predicate} AND {filter_sql(category,table)})'
        if table=='three_platform_store_rank_monthly':
            if not scope:raise BrandQueryError('missing_platform_scope')
            if scope.platform!='TTL':
                from bot.platforms import platform_filter_sql
                predicate=f'({predicate} AND {platform_filter_sql(table,scope.platform)})'
    elif category != 'TTL':
        if table not in platforms:raise BrandQueryError('category_filter_not_migrated')
        from bot.brand_categories import category_case
        params[prefix+'_category']=category
        predicate=f'({predicate} AND ({category_case(platforms[table])}) = :{prefix}_category)'
    return {'sql':predicate,
            'params':params,'audit':{k:result.get(k) for k in ('brand_id','category','mapping_ids','version','cache_state')},
            'values':list(values)}


def apply_brand_predicate(sql, params, *, brand, table, field, category=None,
                          predicate='brand_name = :brand'):
    """Replace only an explicitly declared predicate in an owned SQL template."""
    if predicate not in sql:raise BrandQueryError('template_predicate_missing')
    mapped=brand_predicate(brand,table,field,category=category)
    if any(k in params for k in ('current_start','focus_start')):validate_query_dates(params)
    if set(mapped['params']) & set(params):raise BrandQueryError('parameter_collision')
    bound=dict(params);bound.pop('brand',None);bound.update(mapped['params'])
    from bot.store_report_scope import DAILY,MONTHLY
    if table in DAILY or table==MONTHLY:
        from bot.tools.market_common import store_rank_core_category_sql
        # This broad pre-filter must not discard standalone MEX subtotal rows.
        sql=sql.replace(store_rank_core_category_sql(), '1=1')
    return sql.replace(predicate,mapped['sql']),bound,mapped['audit']


def validate_query_dates(params):
    scope=_scope.get()
    if not scope:return
    from bot.utils import parse_ec_period
    current=params.get('current_start') or params.get('focus_start')
    if not current:raise BrandQueryError('missing_query_dates')
    expected=parse_ec_period(scope.period,int(str(current)[:4]))
    for key in ('current_start','current_end','prior_start','prior_end'):
        actual=params.get(key,params.get(key.replace('current','focus')))
        if actual is not None and str(actual)!=str(expected[key]):
            raise BrandQueryError('period_scope_mismatch')


def require_platform(platform):
    scope=_scope.get()
    if scope is None:raise BrandQueryError('missing_task_scope')
    if scope.platform not in (platform,'TTL'):raise BrandQueryError('platform_scope_mismatch')


def source_fetch(fetcher, table, field, predicate, sql, params):
    """Adapter for an explicitly identified source in an owned query template."""
    if enabled():
        sql,params,_=apply_brand_predicate(sql,params,brand=params['brand'],table=table,
                                          field=field,predicate=predicate)
    return fetcher(sql,params)


def require_total_scope():
    scope=_scope.get()
    if scope and scope.category not in (None,'TTL'):
        raise BrandQueryError('report_requires_total_scope')
