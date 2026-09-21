"""Brand/category clarification only; never generates analysis reports."""
import os
from dataclasses import asdict
from bot.brand_mapping import BrandContext, BrandMapping
from bot.brand_lookup_runtime import get_brand_lookup

ROUTES={'default_chain':'TM','douyin_business_analysis':'DY','jd_business_analysis':'JD','three_platform_competitor_analysis':'TTL','brand_business_investment_analysis':None}

CATEGORIES={'TOTAL BEAUTY':'TTL','FEMALE SKINCARE':'SKIN','MALE SKINCARE':'MEX','SKIN':'SKIN','护肤':'SKIN','女士护肤':'SKIN','MEX':'MEX','男士':'MEX','男士护肤':'MEX',
            'HAIR':'HAIR','洗护':'HAIR','MAKEUP':'MAKEUP','彩妆':'MAKEUP','TTL':'TTL','全部':'TTL','全部生意':'TTL'}

def pending_category_route(session, text):
    """Only exact slot answers may consume a saved clarification."""
    if os.environ.get('BRAND_QUERY_GATE_ENABLED', '0') != '1':
        return None
    saved = dict(session.brand_lookup_context or {})
    category = CATEGORIES.get(text.strip())
    if not saved.get('awaiting_category') or not category:
        return None
    from bot.router import RouteResult
    values=dict(saved.get('route') or {})
    values.update(type=values.get('type') or {'TM':'default_chain','DY':'douyin_business_analysis','JD':'jd_business_analysis','TTL':'three_platform_competitor_analysis'}[saved['platform']],
                  brand=saved.get('input'),period=saved.get('period'),platform=saved.get('platform'),category=category)
    return RouteResult(**values)



def confirm_route(route,session,text):
    if os.environ.get('BRAND_QUERY_GATE_ENABLED','0')!='1':return None
    saved=dict(session.brand_lookup_context or {})
    reply_category=CATEGORIES.get(text.strip())
    pending=saved.get('awaiting_category') and reply_category
    if route.type not in ROUTES and not pending:return None
    if not pending and not route.brand:return None
    lookup=get_brand_lookup()
    if lookup is None:
        return {'ok':False,'markdown':'统一品牌字典暂不可用，请稍后重试。','meta':{'document_ready':False}},saved
    brand=saved.get('input') if pending else route.brand
    period=saved.get('period') if pending else route.period
    platform=saved.get('platform') if pending else (ROUTES.get(route.type) or route.platform)
    category=reply_category if pending else CATEGORIES.get(route.category or '')
    context=BrandContext(**saved['context']) if pending and saved.get('context') else None
    resolved=BrandMapping(lookup.repository.mapping_rows()).resolve(brand,category,context)
    next_state={'input':brand,'period':period,'platform':platform,'route':route.to_dict()}
    if resolved['status']=='clarify_category':
        next_state['awaiting_category']=True
        return {'ok':False,'markdown':f'你想看{brand}的哪个品类？护肤、男士、洗护、彩妆，还是全部生意？',
                'meta':{'document_ready':False,'awaiting':'business_category','brand_id':resolved['brand_id']}},next_state
    if resolved['status']!='resolved':
        return {'ok':False,'markdown':'品牌或品类映射需要核验，请明确品牌和品类。','meta':{'document_ready':False,'brand_mapping':resolved}},next_state
    context=resolved['context'];next_state['context']=asdict(context)
    if not period:
        return {'ok':False,'markdown':'请提供需要分析的时间段。','meta':{'document_ready':False}},next_state
    route.brand, route.period, route.platform, route.category = brand, period, platform, context.category
    return None,next_state


def explicit_category(text):
    """Recognize scope phrases, not brand substrings or negated requests."""
    import re
    text=text.strip()
    exact=CATEGORIES.get(text) or CATEGORIES.get(text.upper())
    if exact:return True,exact
    patterns={
        'TTL':r'全部生意|全部品类|所有品类|全品类|(?<![A-Za-z0-9])TOTAL BEAUTY(?![A-Za-z0-9])',
        'MEX':r'男士(?:护肤)?|(?<![A-Za-z0-9])MEX(?![A-Za-z0-9])',
        'SKIN':r'女士护肤|(?<!男士)护肤|(?<![A-Za-z0-9])SKIN(?:CARE)?(?![A-Za-z0-9])',
        'HAIR':r'洗护|(?<![A-Za-z0-9])HAIR(?![A-Za-z0-9])',
        'MAKEUP':r'彩妆|(?<![A-Za-z0-9])MAKEUP(?![A-Za-z0-9])',
    }
    values=set();mentioned=False;negated=False
    for category,pattern in patterns.items():
        for match in re.finditer(pattern,text,re.I):
            mentioned=True
            if re.search(r'(不要|不看|不是|排除|不含|除去|不包括).{0,3}$',text[:match.start()]):
                negated=True
                continue
            values.add(category)
    if 'TTL' in values and negated:return True,None
    return mentioned,next(iter(values)) if len(values)==1 else None


def merge_route_category(route, session, text):
    """Reconcile only EC category before context mutation; never change routing."""
    if os.environ.get('BRAND_QUERY_GATE_ENABLED','0')!='1' or route.type not in ROUTES:
        return
    mentioned,explicit=explicit_category(text)
    task=session.task_context
    saved=session.brand_lookup_context or {}
    decision=route.route_decision or {}
    relation=decision.get('relation') or (decision.get('task_context_patch') or {}).get('relation')
    platform=ROUTES.get(route.type) or route.platform
    same_brand=bool(task.brand and route.brand and task.brand.strip().casefold()==route.brand.strip().casefold())
    same_platform=platform==task.platform
    previous=CATEGORIES.get(task.category or '')
    # Saved confirmation is scoped to this exact brand, not the last report's
    # selected product subcategory (which may have a different taxonomy).
    if saved.get('input','').strip().casefold()==str(route.brand or '').strip().casefold() and saved.get('platform')==platform:
        previous=CATEGORIES.get((saved.get('context') or {}).get('category','')) or previous
    continuing=relation in {'MODIFY_SCOPE','SUPPLY_MISSING_SLOT','ADD_GOAL','FOLLOW_UP_RESULT','CONTINUE_TASK'}
    requested=CATEGORIES.get(route.category or '') or CATEGORIES.get(str(route.category or '').upper())
    if mentioned:
        category=explicit  # Conflicting/negated-only scopes remain unresolved.
    elif same_brand and same_platform and continuing:
        category=previous  # Do not accept an unsupported new category from LLM.
    elif task.brand:
        category=None  # New brand cannot silently inherit the old category.
    else:
        category=requested
    route.category=category
    if decision.get('task_context_patch'):
        decision['task_context_patch'].setdefault('slot_ops',{})['category']={
            'op':'SET' if category is not None else 'CLEAR','value':category,
            'source':'explicit_category' if mentioned else 'confirmed_scope_merge',
        }
    if decision.get('task_context') is not None:decision['task_context']['category']=category
    route.route_decision=decision
