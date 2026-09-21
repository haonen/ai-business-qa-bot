"""Discover verified daily coverage for the existing three-platform report."""
from datetime import date,timedelta
import re
from bot.db.connection import fetch_df

LABELS={'TM':'天猫','DY':'抖音','JD':'京东'}
LOOKBACK=90

def prior(d):
    try:return d.replace(year=d.year-1)
    except ValueError:return d.replace(year=d.year-1,day=28)

def derive_options(available,today):
    """Require every requested day and its calendar-year comparison day."""
    common=set.intersection(*(set(v) for v in available.values())) if available else set()
    valid={d for d in common if today-timedelta(days=LOOKBACK)<=d<today and prior(d) in common}
    options={}
    if valid:
        latest=max(valid);options['day']={'start':latest.isoformat(),'end':latest.isoformat()}
    # Only complete Mon-Sun weeks, all 7 current and 7 comparison days present.
    for end in sorted((d for d in valid if d.weekday()==6),reverse=True):
        start=end-timedelta(days=6)
        if all(start+timedelta(days=i) in valid for i in range(7)):
            options['week']={'start':start.isoformat(),'end':end.isoformat()};break
    return options

def _discover_window(brand,today=None,window=14):
    from bot.brand_reference import english_brand_for_chinese
    from bot.tools.query_three_platform_competitor import PLATFORM_CONFIG,_date_contract
    from bot.platforms import normalized_platform_sql
    today=today or date.today();start=today-timedelta(days=window);end=today-timedelta(days=1)
    aliases=list(dict.fromkeys([brand,english_brand_for_chinese(brand) or brand]))
    bindings={f'brand_{i}':b for i,b in enumerate(aliases)}
    brands=','.join(f':brand_{i}' for i in range(len(aliases)))
    available={};latest={}
    for platform,cfg in PLATFORM_CONFIG.items():
        table=cfg['daily_table'];days=set()
        # Separate years and preserve native indexed fields in WHERE/GROUP BY.
        for lo,hi in [(start,end),(prior(start),prior(end))]:
            values={'lo':lo.isoformat(),'hi':hi.isoformat()}
            if platform=='TM':values={k:v.replace('-','/') for k,v in values.items()}
            params=dict(bindings,**values)
            frame=fetch_df(f"SELECT /*+ MAX_EXECUTION_TIME(5000) */ bus_date AS day FROM {table} WHERE brand_name IN ({brands}) AND bus_date BETWEEN :lo AND :hi GROUP BY bus_date",params)
            days.update(date.fromisoformat(str(x)[:10].replace('/','-')) for x in frame.get('day',[]) if str(x) not in ('None','NaT'))
        available[platform]=days
        recent=[d for d in days if start<=d<=end];latest[platform]=max(recent).isoformat() if recent else None
    # Same optional mass-market prerequisite as the report, including aliases.
    mass=fetch_df(f"SELECT /*+ MAX_EXECUTION_TIME(5000) */ 1 AS found FROM three_platform_store_rank_monthly WHERE brand_name IN ({brands}) AND (SELECTIVITY IS NULL OR TRIM(SELECTIVITY)='') LIMIT 1",bindings)
    segments=['BEAUTY MARKET']+(['PURE MASS'] if not mass.empty else [])
    table='three_platforms_segmented_markets_daily';platform_expr=normalized_platform_sql(table)
    for segment in segments:
        for platform in LABELS:available[segment+platform]=set()
        for lo,hi in [(start,end),(prior(start),prior(end))]:
            frame=fetch_df(f"SELECT /*+ MAX_EXECUTION_TIME(5000) */ bus_date AS day, {platform_expr} AS platform FROM {table} WHERE UPPER(TRIM(global_segment))=:segment AND bus_date BETWEEN :lo AND :hi GROUP BY bus_date, {platform_expr}",{'segment':segment,'lo':lo.isoformat(),'hi':hi.isoformat()})
            for platform in LABELS:
                available[segment+platform].update(date.fromisoformat(str(row['day'])[:10]) for _,row in frame.iterrows() if row['platform']==platform)
    return {'brand':brand,'platform':'TTL','checked_on':today.isoformat(),'latest_by_platform':latest,'options':derive_options(available,today),'lookback_days':window}


def discover(brand,today=None):
    result=None
    for window in (14,42,90):
        try:
            candidate=_discover_window(brand,today,window)
        except Exception:
            # A wider search failing must not discard already verified choices.
            if result and result.get('options'):
                result['search_incomplete']=True
                return result
            raise
        result=candidate
        if all(key in result['options'] for key in ('day','week')):break
    return result


def describe(result):
    if result.get('error'):
        return '暂时未能核实最新可用日期。你可以回复“最新可查日期”重试，我会保留当前品牌和三平台范围。'
    options=result.get('options') or {};lines=[]
    searched=result.get('lookback_days',LOOKBACK)
    if options.get('day'):
        lines.append(f"{result['brand']}三平台近{searched}天内最近可分析的一天是 **{options['day']['start']}**（本期、同比同期及所需大盘数据均有记录）。")
        lines.append('回复 **“最近一天”**，即可按这个日期分析。')
    if options.get('week'):
        w=options['week'];lines.append(f"最近完整一周：**{w['start']}至{w['end']}**。回复 **“最近一周”** 即可分析。")
    if not options:
        lines.append(f"近{searched}天内暂未找到三平台及同比、大盘数据都齐全的可分析日期。")
    latest=result.get('latest_by_platform') or {}
    lines.append('各平台品牌数据最近记录：'+'；'.join(LABELS[p]+' '+(latest.get(p) or f'近{searched}天未找到') for p in LABELS)+'。')
    if result.get('search_incomplete'):lines.append('更早日期的核对暂未完成，以上选项已核实。')
    return '\n\n'.join(lines)

def safe_discover(brand):
    try:return discover(brand)
    except Exception:
        import logging
        logging.getLogger(__name__).exception('coverage discovery failed')
        return {'brand':brand,'platform':'TTL','error':'coverage_unavailable','options':{}}

ASK=re.compile(r'^(?:那|请问|请|帮我|现在|目前)?(?:最新(?:数据)?(?:到|能查到|可查|更新到)?(?:什么日期|哪个日期|哪天|什么时候|什么时间|几号|哪一天|日期)?|数据(?:最新)?(?:到|更新到|能查到)(?:什么日期|哪天|什么时候|几号))[？?。！!]*$')
DAY={'最近一天','最新一天','那就最近一天','就看最近一天','分析最近一天','看最近一天'}
WEEK={'最近一周','最近完整一周','那就最近一周','分析最近一周','看最近一周'}

def handle_followup(open_id,text,state):
    """Intercept coverage questions before date clarification; never change scope silently."""
    from bot.session import set_pending_request
    pending=dict(state.pending_request or {});frame=state.task_context
    brand=pending.get('brand') or frame.brand
    platform=pending.get('platform') or frame.platform
    compact=re.sub(r'\s+','',text).strip('？?。！!')
    selection='day' if compact in DAY else ('week' if compact in WEEK else None)
    if not brand or platform!='TTL' or not(selection or ASK.fullmatch(compact)):return None
    saved=pending.get('coverage_options') or {}
    if selection and saved.get('brand')==brand and saved.get('checked_on')==date.today().isoformat() and saved.get('options',{}).get(selection):
        choice=saved['options'][selection]
        return {'resolved_text':f"分析{brand}三平台{choice['start']}至{choice['end']}的生意"}
    result=safe_discover(brand)
    pending.update(brand=brand,platform='TTL',coverage_options=result)
    set_pending_request(open_id,pending)
    # Newly discovered choices require a reply, so dates are visible before execution.
    return {'markdown':describe(result),'route_type':'data_availability','route':{'type':'data_availability'},'meta':{'brand':brand,'platform':'TTL','document_ready':False,'coverage_options':result}}
