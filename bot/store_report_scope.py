"""Source-specific store aggregation for existing Bot report templates."""
from bot.store_metric_scope import weight_sql
DAILY={'tmall_store_ranking_day_jiashicang':'TM','dy_store_ranking_BFSS_day_jiashicang':'DY','jd_store_ranking_selfrun_day_jiashicang':'JD'}
MONTHLY='three_platform_store_rank_monthly'

def source_weight(category,table):
    if table in DAILY:return weight_sql(category,DAILY[table])
    if table!=MONTHLY:raise ValueError('Not a store source')
    return "(CASE WHEN UPPER(TRIM(platform))='JD' THEN "+weight_sql(category,'JD')+' ELSE '+weight_sql(category,'TM')+' END)'

def filter_sql(category,table):
    return '('+source_weight(category,table)+' <> 0)'

def amount_sql(expression,platform):
    from bot.brand_query import enabled,_scope
    scope=_scope.get()
    # MEX/HAIR/MAKEUP have positive weights; SKIN subtracts independent male subtotals.
    if enabled() and scope and scope.category=='SKIN':
        return f'(({expression}) * {weight_sql("SKIN",platform)})'
    return expression

def monthly_category_sql():
    from bot.brand_query import enabled,_scope
    scope=_scope.get()
    if enabled() and scope and scope.category=="SKIN":return "'SKIN'"
    return "CASE WHEN LOWER(TRIM(COALESCE(category_EN_level_2,'')))='male skincare' THEN 'MEX' WHEN LOWER(TRIM(category_EN_level_1))='skincare' THEN 'SKIN' WHEN LOWER(TRIM(category_EN_level_1))='hair' THEN 'HAIR' WHEN LOWER(TRIM(category_EN_level_1)) IN ('makeup','makeup + fragrance','makeup+fragrance','makeup (exclude fragrance)') THEN 'MAKEUP' ELSE '未分类' END"
