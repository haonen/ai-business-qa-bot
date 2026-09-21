"""Section01 atomic market facts; no brand/product queries or model arithmetic."""
from datetime import date
from decimal import Decimal
from bot.newsletter.data import periods,date_set
from bot.db.connection import fetch_df

SOURCE='three_platforms_segmented_markets_daily'
PLATFORMS=('TM','JD','DY')
SEGMENTS=('BEAUTY MARKET','PURE MASS')

def query_market_metrics(start,end,platform='TTL',segment='BOTH',*,fetcher=None):
    if platform not in (*PLATFORMS,'TTL') or segment not in (*SEGMENTS,'BOTH'):
        raise ValueError('unsupported market scope')
    params=periods(start,end)
    if (date.fromisoformat(end)-date.fromisoformat(start)).days>=366:
        raise ValueError('查询期间请限制在366天以内。')
    pls=PLATFORMS if platform=='TTL' else (platform,)
    segments=SEGMENTS if segment=='BOTH' else (segment,)
    binds={**params,**{f'p{i}':p for i,p in enumerate(pls)},**{f's{i}':s for i,s in enumerate(segments)}}
    # Separate period labels also handle overlapping dates without ambiguity.
    predicate='UPPER(TRIM(platform)) IN ('+','.join(':p'+str(i) for i in range(len(pls)))+') AND UPPER(TRIM(global_segment)) IN ('+','.join(':s'+str(i) for i in range(len(segments)))+')'
    amount="REPLACE(TRIM(CAST(gmv AS CHAR)),',','')"
    valid=f"{amount} REGEXP '^[+-]?[0-9]+([.][0-9]+)?$'"
    parts=[]
    for per,a,b in [('current','cs','ce'),('prior','ps','pe')]:
        parts.append(f"""SELECT '{per}' period_key, CAST(bus_date AS DATE) day,
UPPER(TRIM(platform)) platform, UPPER(TRIM(global_segment)) segment,
SUM(CASE WHEN {valid} THEN CAST({amount} AS DECIMAL(30,6)) ELSE 0 END) gmv,
SUM(CASE WHEN {valid} THEN 0 ELSE 1 END) invalid_rows
FROM {SOURCE} WHERE bus_date BETWEEN :{a} AND :{b} AND {predicate}
GROUP BY CAST(bus_date AS DATE),UPPER(TRIM(platform)),UPPER(TRIM(global_segment))""")
    sql=' UNION ALL '.join(parts)
    frame=(fetcher or fetch_df)(sql,binds)
    rows=frame.to_dict('records')
    totals={};days={};invalid=set()
    for row in rows:
        key=(row['period_key'],row['segment']);coverage=(row['period_key'],row['segment'],row['platform'])
        if row['platform'] not in pls or row['segment'] not in segments or row['period_key'] not in ('current','prior'):
            raise ValueError('unexpected source scope')
        totals[key]=totals.get(key,Decimal(0))+Decimal(str(row['gmv']))
        days.setdefault(coverage,set()).add(str(row['day'])[:10])
        if row.get('invalid_rows'):invalid.add(key)
    metrics=[]
    for seg in segments:
        missing={}
        for per,a,b in [('current','cs','ce'),('prior','ps','pe')]:
            missing[per]={p:sorted(date_set(params[a],params[b])-days.get((per,seg,p),set())) for p in pls}
            missing[per]={p:d for p,d in missing[per].items() if d}
        current=totals.get(('current',seg),Decimal(0)) if not missing['current'] and ('current',seg) not in invalid else None
        prior=totals.get(('prior',seg),Decimal(0)) if not missing['prior'] and ('prior',seg) not in invalid else None
        evol=(current/prior-1)*100 if current is not None and prior is not None and prior>0 else None
        metrics.append(dict(segment=seg,current_gmv_yuan=str(current) if current is not None else None,
            prior_gmv_yuan=str(prior) if prior is not None else None,evol_pct=str(evol) if evol is not None else None,
            missing_dates=missing,invalid_amount_periods=[p for p in ('current','prior') if (p,seg) in invalid]))
    return dict(schema='newsletter.overall_market.v1',platform=platform,period=params,metrics=metrics,
                source=SOURCE,scope_policy='newsletter.section01.v1',mode='live',sql=sql,params=binds)
