"""Newsletter 01 CPD observed store GMV and Pure Mass share, without report generation."""
from datetime import date
from decimal import Decimal
from bot.newsletter.data import TABLES,OWNED,periods,date_set
from bot.store_metric_scope import weight_sql
from bot.db.connection import fetch_df


def query_cpd_metrics(start,end,platform='TTL',*,fetcher=None):
    if platform not in ('TTL',*TABLES):raise ValueError('unsupported CPD platform')
    params=periods(start,end)
    if (date.fromisoformat(end)-date.fromisoformat(start)).days>=366:
        raise ValueError('查询期间请限制在366天以内。')
    platforms=tuple(TABLES) if platform=='TTL' else (platform,)
    binds={**params,**{f'brand{i}':brand for i,brand in enumerate(sorted(OWNED))}}
    owned='UPPER(TRIM(brand_name)) IN ('+','.join(':brand'+str(i) for i in range(len(OWNED)))+')'
    amount="REPLACE(TRIM(CAST(gmv AS CHAR)),',','')"
    valid=f"{amount} REGEXP '^[+-]?[0-9]+([.][0-9]+)?$'"
    totals={'current':Decimal(0),'prior':Decimal(0)};days={};invalid=set()
    for pl in platforms:
        # Scan the eligible source pool, not only CPD: absent observed CPD is 0,
        # but absence of the entire source day must remain missing.
        parts=[]
        for per,a,b in [('current','cs','ce'),('prior','ps','pe')]:
            parts.append(f"""SELECT '{per}' period_key,CAST(bus_date AS DATE) day,
SUM(CASE WHEN {owned} AND {valid} THEN CAST({amount} AS DECIMAL(30,6)) ELSE 0 END) gmv,
SUM(CASE WHEN {owned} AND NOT COALESCE(({valid}),0) THEN 1 ELSE 0 END) invalid_rows
FROM `{TABLES[pl]}` WHERE bus_date BETWEEN :{a} AND :{b}
AND ({weight_sql('TTL',pl)}) > 0
GROUP BY CAST(bus_date AS DATE)""")
        for row in (fetcher or fetch_df)(' UNION ALL '.join(parts),binds).to_dict('records'):
            per=row['period_key']
            if per not in totals:raise ValueError('unexpected CPD period')
            totals[per]+=Decimal(str(row['gmv']))
            days.setdefault((per,pl),set()).add(str(row['day'])[:10])
            if row.get('invalid_rows'):invalid.add(per)
    missing={}
    for per,a,b in [('current','cs','ce'),('prior','ps','pe')]:
        missing[per]={pl:sorted(date_set(params[a],params[b])-days.get((per,pl),set())) for pl in platforms}
        missing[per]={pl:ds for pl,ds in missing[per].items() if ds}
    now=totals['current'] if not missing['current'] and 'current' not in invalid else None
    old=totals['prior'] if not missing['prior'] and 'prior' not in invalid else None
    evol=(now/old-1)*100 if now is not None and old is not None and old>0 else None
    return dict(current_gmv_yuan=str(now) if now is not None else None,
                prior_gmv_yuan=str(old) if old is not None else None,
                evol_pct=str(evol) if evol is not None else None,missing_dates=missing,
                invalid_amount_periods=sorted(invalid),brands=sorted(OWNED),
                sources={pl:TABLES[pl] for pl in platforms},scope_policy='newsletter.section01.cpd_observed.v1')


def share_metrics(cpd,pure_mass):
    result={'denominator':'PURE MASS'}
    for per in ('current','prior'):
        numerator=cpd.get(per+'_gmv_yuan');denominator=pure_mass.get(per+'_gmv_yuan')
        share=Decimal(numerator)/Decimal(denominator)*100 if numerator is not None and denominator is not None and Decimal(denominator)>0 else None
        result[per+'_share_pct']=str(share) if share is not None else None
    now,old=result['current_share_pct'],result['prior_share_pct']
    result['change_pp']=str(Decimal(now)-Decimal(old)) if now is not None and old is not None else None
    return result
