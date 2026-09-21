"""Latest complete seven-day male-skincare window from the actual Tmall source."""
from datetime import date,timedelta
from collections import defaultdict
from .data import periods,date_set,CoverageError,TABLES,category_weights
from .category_market import query as market_query,calculate as market_calculate

def discover(params,run):
    end=date.fromisoformat(params['ce']);start=end-timedelta(days=41)
    rows=run('section02_mex_available_dates',"""SELECT DISTINCT CAST(bus_date AS DATE) day
FROM `天猫_大盘_日表` WHERE bus_date BETWEEN :start AND :end
AND LOWER(TRIM(category_EN_level_2))='male skincare' ORDER BY day DESC""",dict(start=str(start),end=str(end)))
    days={str(r['day'])[:10] for r in rows};latest=max(days,default=None)
    for ending in sorted(days,reverse=True):
        first=str(date.fromisoformat(ending)-timedelta(days=6))
        if date_set(first,ending)<=days:
            return dict(params=periods(first,ending),latest_observed=latest,window_days=7,
                        source='天猫_大盘_日表',field='category_EN_level_2',value='male skincare')
    raise CoverageError([dict(source='section02_mex_window',platform='TM',category='MEX',period='current',missing_dates=[],reason='no_complete_seven_day_window',latest_observed=latest)])

def query(window,run):
    params=window['params']
    raw=market_query(params,lambda name,sql,args:run('mex_'+name,sql,args),TABLES)
    brands={}
    for pl,table in TABLES.items():
        amount="REPLACE(TRIM(CAST(gmv AS CHAR)),',','')"
        valid=f"{amount} REGEXP '^[+-]?[0-9]+([.][0-9]+)?$'"
        brands[pl]=run('section02_mex_brands_'+pl,f"""SELECT CAST(bus_date AS DATE) day,brand_name,category_EN_level_1,category_EN_level_2,
SUM(CASE WHEN {valid} THEN CAST({amount} AS DECIMAL(30,6)) ELSE 0 END) gmv,
SUM(CASE WHEN {valid} THEN 0 ELSE 1 END) invalid_rows
FROM `{table}` WHERE (bus_date BETWEEN :cs AND :ce OR bus_date BETWEEN :ps AND :pe)
AND UPPER(TRIM(brand_name))="L'OREAL PARIS"
GROUP BY CAST(bus_date AS DATE),brand_name,category_EN_level_1,category_EN_level_2""",params)
    return dict(window=window,raw=raw,brands=brands)

def metrics(bundle,categories):
    params=bundle['window']['params']
    denominators,missing=market_calculate(bundle['raw'],params,category_weights,date_set,categories=categories)
    if missing:raise CoverageError(missing)
    owned=defaultdict(float)
    for pl,rows in bundle['brands'].items():
        for row in rows:
            if row.get('invalid_rows'):raise ValueError('Invalid independent-window brand amount')
            day=str(row['day'])[:10];per='current' if params['cs']<=day<=params['ce'] else 'prior'
            for cat,sign in category_weights(row,pl):owned[per,cat]+=float(row['gmv'])*sign
    return denominators,owned
